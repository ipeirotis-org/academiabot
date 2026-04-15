from dotenv import load_dotenv
import os
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

console = Console()


def require_key(name: str, val) -> str:
    if not val:
        raise ValueError(f"{name} not set in environment")
    return val
