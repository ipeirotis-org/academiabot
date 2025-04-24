from dotenv import load_dotenv
import os
import sys
from rich.console import Console

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
USER_AGENT = os.getenv("WD_BOT_USERAGENT", "AcademiaBot/1.0 (ipeirotis@example.com)")
LLM_MODEL = "gpt-4.1"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

if not OPENAI_API_KEY:
    sys.exit("OPENAI_API_KEY not set.")

console = Console()