import time
import logging
import requests
from typing import List, Tuple
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from .config import USER_AGENT
from .sparql_helpers import _retry_after_seconds

logger = logging.getLogger(__name__)

# Polite pause between Wikidata API requests (matches SPARQL pause)
_WD_API_DELAY = 0.3
_SEARCH_URL = "https://www.wikidata.org/w/api.php"


class WikidataRateLimited(Exception):
    """Raised after waiting out an HTTP 429 from the Wikidata API."""


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((requests.RequestException, WikidataRateLimited)),
    before_sleep=lambda rs: logger.warning(
        "Wikidata search retry #%d after %s", rs.attempt_number, rs.outcome.exception()
    ),
)
def quick_wd_search(label: str, language: str = "en") -> List[Tuple[str, str]]:
    """Search Wikidata labels and aliases in `language`. Honors Retry-After on 429
    instead of failing fast."""
    time.sleep(_WD_API_DELAY)
    resp = requests.get(
        _SEARCH_URL,
        params={"action": "wbsearchentities", "format": "json", "language": language, "limit": 10, "search": label},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    if resp.status_code == 429:
        wait = _retry_after_seconds(resp.headers.get("Retry-After"))
        logger.warning("Wikidata search 429 for '%s'. Waiting %ss.", label, wait)
        time.sleep(wait)
        raise WikidataRateLimited(label)
    resp.raise_for_status()
    hits = resp.json().get("search", [])
    return [(h["id"], h["label"]) for h in hits]
