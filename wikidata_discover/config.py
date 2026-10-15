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

# Run log (wikidata_discover/run_log.py): BigQuery dataset and bucket. Override them
# to test the local fallback, for example ACADEMIABOT_BUCKET=no-such-bucket.
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "wikidata-academia")
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
    """Make every API key available that can be found: environment first, then .env,
    then Secret Manager for each key still missing. Secret Manager is not contacted
    when all three keys are set locally. Returns {key name: "env" | "dotenv" |
    "secret_manager" | "missing"}. Raises RuntimeError when no key is found at all."""
    sources = key_sources_before_secret_manager()
    if "missing" not in sources.values():
        return sources
    from wikidata_discover.batch import load_keys_from_secret_manager
    fetched = load_keys_from_secret_manager()   # fills only the missing keys
    return {name: (sources[name] if sources[name] != "missing" else fetched.get(name, "missing"))
            for name in API_KEY_NAMES}


def require_key(name: str, val) -> str:
    if not val:
        raise ValueError(f"{name} not set in environment")
    return val
