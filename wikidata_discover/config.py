from dotenv import load_dotenv
import os
from pathlib import Path
from rich.console import Console

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

USER_AGENT = os.getenv("WD_BOT_USERAGENT", "AcademiaBot/1.0 (ipeirotis@example.com)")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# All output files (CSV, QuickStatements, harvest JSON, reports, LLM cache) go here
RESULTS_DIR = Path(__file__).parent / "results"

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


def require_key(name: str, val) -> str:
    if not val:
        raise ValueError(f"{name} not set in environment")
    return val
