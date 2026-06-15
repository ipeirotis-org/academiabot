import time
import logging
import requests
from typing import List, Tuple, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from .config import USER_AGENT

logger = logging.getLogger(__name__)

# Polite pause between Wikidata API requests (matches SPARQL pause)
_WD_API_DELAY = 0.3


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((requests.RequestException,)),
    before_sleep=lambda rs: logger.warning(
        "Wikidata search retry #%d after %s", rs.attempt_number, rs.outcome.exception()
    ),
)
def quick_wd_search(label: str) -> List[Tuple[str, str]]:
    time.sleep(_WD_API_DELAY)
    url = (
        "https://www.wikidata.org/w/api.php?"
        "action=wbsearchentities&format=json&language=en&limit=10&search="
        + requests.utils.quote(label)
    )
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("search", [])
    return [(h["id"], h["label"]) for h in hits]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((requests.RequestException,)),
    before_sleep=lambda rs: logger.warning(
        "Wikidata entity retry #%d after %s", rs.attempt_number, rs.outcome.exception()
    ),
)
def get_entity_label_and_website(qid: str) -> Tuple[str, Optional[str]]:
    """Fetch an entity label and official website via the Wikidata Action API."""
    time.sleep(_WD_API_DELAY)
    resp = requests.get(
        "https://www.wikidata.org/w/api.php",
        params={
            "action": "wbgetentities",
            "format": "json",
            "ids": qid,
            "props": "labels|claims",
            "languages": "en",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    entity = resp.json().get("entities", {}).get(qid, {})
    label = entity.get("labels", {}).get("en", {}).get("value")
    website = None
    website_claims = entity.get("claims", {}).get("P856", [])
    if website_claims:
        website = (
            website_claims[0]
            .get("mainsnak", {})
            .get("datavalue", {})
            .get("value")
        )
    if not label:
        raise ValueError(f"Label not found for {qid}")
    return label, website
