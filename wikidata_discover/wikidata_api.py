import time
import logging
import requests
from typing import List, Tuple
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from . import config
from .sparql_helpers import _retry_after_seconds, bounded_wait, check_deadline, past_deadline, request_timeout

logger = logging.getLogger(__name__)

# Polite pause between Wikidata API requests (matches SPARQL pause)
_WD_API_DELAY = 0.3
_SEARCH_URL = "https://www.wikidata.org/w/api.php"


class WikidataRateLimited(Exception):
    """Raised after waiting out an HTTP 429 from the Wikidata API."""


@retry(
    stop=lambda rs: stop_after_attempt(4)(rs) or past_deadline(rs),
    wait=lambda rs: bounded_wait(wait_exponential(multiplier=1, min=2, max=30)(rs)),
    retry=retry_if_exception_type((requests.RequestException, WikidataRateLimited)),
    before_sleep=lambda rs: logger.warning(
        "Wikidata search retry #%d after %s", rs.attempt_number, rs.outcome.exception()
    ),
)
def quick_wd_search(label: str, language: str = "en") -> List[Tuple[str, str]]:
    """Search Wikidata labels and aliases in `language`. Honors Retry-After on 429
    instead of failing fast."""
    check_deadline("a Wikidata search")
    time.sleep(bounded_wait(_WD_API_DELAY))
    check_deadline("a Wikidata search")   # the polite pause may have crossed the deadline
    resp = requests.get(
        _SEARCH_URL,
        params={"action": "wbsearchentities", "format": "json", "language": language, "limit": 10, "search": label},
        headers={"User-Agent": config.USER_AGENT},
        timeout=request_timeout(30),
    )
    if resp.status_code == 429:
        wait = bounded_wait(_retry_after_seconds(resp.headers.get("Retry-After")))
        logger.warning("Wikidata search 429 for '%s'. Waiting %ss.", label, wait)
        time.sleep(wait)
        raise WikidataRateLimited(label)
    resp.raise_for_status()
    hits = resp.json().get("search", [])
    return [(h["id"], h["label"]) for h in hits]


@retry(
    stop=lambda rs: stop_after_attempt(4)(rs) or past_deadline(rs),
    wait=lambda rs: bounded_wait(wait_exponential(multiplier=1, min=2, max=30)(rs)),
    retry=retry_if_exception_type((requests.RequestException, WikidataRateLimited)),
    before_sleep=lambda rs: logger.warning(
        "Wikidata labels retry #%d after %s", rs.attempt_number, rs.outcome.exception()
    ),
)
def _labels_batch(qids: List[str], language: str) -> dict:
    check_deadline("a Wikidata labels request")
    time.sleep(bounded_wait(_WD_API_DELAY))
    resp = requests.get(
        _SEARCH_URL,
        params={"action": "wbgetentities", "format": "json", "ids": "|".join(qids),
                "props": "labels", "languages": language},
        headers={"User-Agent": config.USER_AGENT},
        timeout=request_timeout(30),
    )
    if resp.status_code == 429:
        wait = bounded_wait(_retry_after_seconds(resp.headers.get("Retry-After")))
        logger.warning("Wikidata labels 429. Waiting %ss.", wait)
        time.sleep(wait)
        raise WikidataRateLimited("labels")
    resp.raise_for_status()
    return resp.json().get("entities", {})


def labels(qids, language: str = "en") -> dict:
    """{qid: label in `language`, or None} for each QID, read with wbgetentities in
    batches of 50 (the API maximum). Read-only. A missing or deleted item maps to None."""
    qids = sorted(set(qids))
    out = {}
    for i in range(0, len(qids), 50):
        for qid, ent in _labels_batch(qids[i:i + 50], language).items():
            out[qid] = ent.get("labels", {}).get(language, {}).get("value")
    return {q: out.get(q) for q in qids}
