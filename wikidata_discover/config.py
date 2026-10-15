from dotenv import load_dotenv
import os
from pathlib import Path
from rich.console import Console

# Variables set before .env is read come from the real environment. load_dotenv never
# overrides them, so the order for each key is: environment, then .env, then Secret
# Manager (load_api_keys below) for a key that neither has.
_ENV_BEFORE_DOTENV = set(os.environ)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
API_KEY_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY")

# Run log (wikidata_discover/run_log.py): BigQuery dataset and bucket, in GCP_PROJECT
# (set below with the Secret Manager names). Override them to test the local
# fallback, for example ACADEMIABOT_BUCKET=no-such-bucket.
RUNLOG_DATASET = os.getenv("ACADEMIABOT_DATASET", "academiabot")
RUNLOG_BUCKET = os.getenv("ACADEMIABOT_BUCKET", "academiabot")

USER_AGENT = os.getenv("WD_BOT_USERAGENT", "AcademiaBot/1.0 (ipeirotis@example.com)")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# All output files (CSV, QuickStatements, harvest JSON, reports, LLM cache) go here
RESULTS_DIR = Path(__file__).parent / "results"

# LLM answer cache. Never move, overwrite, or delete files in results/cache: they are the
# only copy of answers from before the run log. To force fresh LLM calls in a test, set
# ACADEMIABOT_CACHE_DIR to a separate, empty folder instead.
LLM_CACHE_DIR = Path(os.getenv("ACADEMIABOT_CACHE_DIR") or RESULTS_DIR / "cache")

console = Console()


# Absolute time (time.time()) after which no Wikidata request, LLM request, or retry
# wait may start, set by the batch runner when the process has a hard timeout. None
# means no limit. HARD_DEADLINE is the moment the platform kills the process; the gap
# between the two (90 s in the batch runner) is kept for writing and uploading the
# attempt's records.
DEADLINE = None
HARD_DEADLINE = None


def seconds_left() -> float | None:
    """Seconds until DEADLINE, or None when there is no deadline."""
    import time
    return None if DEADLINE is None else DEADLINE - time.time()


def hard_seconds_left() -> float | None:
    """Seconds until the process is killed (HARD_DEADLINE), or None when unknown."""
    import time
    return None if HARD_DEADLINE is None else HARD_DEADLINE - time.time()


def set_user_agent(value: str) -> None:
    """Change the User-Agent for every later Wikidata request, in this process."""
    global USER_AGENT
    USER_AGENT = value
    os.environ["WD_BOT_USERAGENT"] = value


# Where the LLM keys live in GCP Secret Manager. A key that the environment and .env
# do not set is read from here by load_missing_keys().
GCP_PROJECT = "wikidata-academia"
KEY_SECRETS = {
    "OPENAI_API_KEY": "openai-api-key",
    "ANTHROPIC_API_KEY": "anthropic-api-key",
    "GOOGLE_API_KEY": "gemini-api-key",
}
SECRET_TIMEOUT_S = 20


def _new_secret_client():
    from google.cloud import secretmanager
    return secretmanager.SecretManagerServiceClient()


def _secret_call_kwargs() -> dict:
    """One read waits SECRET_TIMEOUT_S at most, retries included."""
    kwargs = {"timeout": SECRET_TIMEOUT_S}
    try:
        from google.api_core import retry as api_retry
        kwargs["retry"] = api_retry.Retry(deadline=SECRET_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - client library absent: the timeout alone
        pass
    return kwargs


def load_missing_keys(client=None) -> dict:
    """Fill each LLM key that the environment and .env do not set from Secret Manager.

    The order is: environment variable, then .env (both already in os.environ, because
    load_dotenv() ran at import), then Secret Manager. Sets the environment variable
    and the module attribute, so code that reads either sees the key. The client is
    built only when a key is missing, so a laptop with keys in .env and no Google
    credentials makes no Google call. A secret that cannot be read is skipped with a
    message; when the client cannot be built (no credentials), no other secret is
    tried. Never prints a key value.
    Returns {env_name: "env" | "secret_manager" | "missing"}."""
    import sys
    module = sys.modules[__name__]
    sources = {}
    no_client = False
    for env, secret in KEY_SECRETS.items():
        if os.getenv(env):
            sources[env] = "env"
            continue
        sources[env] = "missing"
        if no_client:
            continue
        if client is None:
            try:
                client = _new_secret_client()
            except Exception as e:  # noqa: BLE001 - no credentials or library: keys stay missing
                console.print(f"[yellow]No Google credentials for Secret Manager ({type(e).__name__}); "
                              "only keys from the environment or .env are used.[/yellow]")
                no_client = True
                continue
        name = f"projects/{GCP_PROJECT}/secrets/{secret}/versions/latest"
        try:
            value = client.access_secret_version(request={"name": name},
                                                 **_secret_call_kwargs()).payload.data.decode().strip()
        except Exception as e:  # noqa: BLE001 - one unreadable secret must not stop the others
            console.print(f"[yellow]{env} is not set and Secret Manager did not return {secret} "
                          f"({type(e).__name__}).[/yellow]")
            continue
        if value:
            os.environ[env] = value
            setattr(module, env, value)
            sources[env] = "secret_manager"
            console.print(f"[dim]Read {env} from Secret Manager ({secret}).[/dim]")
    return sources


def key_sources_before_secret_manager() -> dict:
    """Where each API key comes from without Secret Manager: "env", "dotenv", or
    "missing". Pure lookup on os.environ, unit tested."""
    out = {}
    for name in API_KEY_NAMES:
        if not os.getenv(name):
            out[name] = "missing"
        else:
            out[name] = "env" if name in _ENV_BEFORE_DOTENV else "dotenv"
    return out


def load_api_keys() -> dict:
    """Make every API key available that can be found, and say where each came from.
    The loading itself is load_missing_keys() above (environment, then .env, then
    Secret Manager). This adds the difference between "env" and "dotenv" for the runs
    row. Returns {key name: "env" | "dotenv" | "secret_manager" | "missing"}."""
    before = key_sources_before_secret_manager()
    after = load_missing_keys()
    return {name: (before[name] if before[name] != "missing" else after.get(name, "missing"))
            for name in API_KEY_NAMES}


def require_key(name: str, val) -> str:
    if not val:
        raise ValueError(f"{name} not set in environment")
    return val
