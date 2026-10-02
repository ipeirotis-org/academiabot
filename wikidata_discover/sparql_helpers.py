import logging
import time
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from wikidata_discover.config import SPARQL_ENDPOINT, USER_AGENT

logger = logging.getLogger(__name__)

# Polite crawling: Wikidata asks for a clear User-Agent and a gentle request rate.
_SPARQL_DELAY = 0.3
_RATE_LIMIT_WAIT = 65  # seconds to wait on HTTP 429 when no Retry-After header is given
_last_call = 0.0


class SparqlRateLimited(Exception):
    """Raised when the endpoint answers HTTP 429 after we have already waited."""


def _parse_bindings(payload: dict) -> list[dict]:
    """Pull the bindings list out of a SPARQL JSON results payload."""
    return payload.get("results", {}).get("bindings", [])


def _get(query: str) -> requests.Response:
    global _last_call
    gap = time.time() - _last_call
    if gap < _SPARQL_DELAY:
        time.sleep(_SPARQL_DELAY - gap)
    resp = requests.get(
        SPARQL_ENDPOINT,
        params={"query": query, "format": "json"},
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
        timeout=120,
    )
    _last_call = time.time()
    return resp


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((requests.RequestException, SparqlRateLimited)),
    before_sleep=lambda rs: logger.warning(
        "SPARQL retry #%d after %s", rs.attempt_number, rs.outcome.exception()
    ),
)
def execute_sparql_bindings(query: str) -> list[dict]:
    """
    Run any SPARQL query and return the full list of result bindings
    (the raw JSON objects) so callers can pull out whatever fields they need.

    Uses plain HTTP GET with the project User-Agent. On HTTP 429 it waits for the
    Retry-After period (or 65 seconds) and tries again instead of failing fast.
    """
    resp = _get(query)
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", _RATE_LIMIT_WAIT) or _RATE_LIMIT_WAIT)
        logger.warning("SPARQL 429 (%s). Waiting %ss.", resp.text.strip()[:80], wait)
        time.sleep(wait)
        raise SparqlRateLimited(resp.text.strip()[:120])
    resp.raise_for_status()
    return _parse_bindings(resp.json())


def run_sparql(query: str, as_tuples: bool = False,
               main_key: str = "univ", label_key: str = "univLabel"):
    """
    Run a SPARQL query. By default return raw dicts.
    If as_tuples=True, convert to (qid, label) pairs.
    """
    bindings = execute_sparql_bindings(query)

    if as_tuples:
        rows = []
        for b in bindings:
            qid = b.get(main_key, {}).get("value", "").rsplit("/", 1)[-1]
            label = b.get(label_key, {}).get("value")
            rows.append((qid, label))
        return rows

    return bindings
