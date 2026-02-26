import logging
from SPARQLWrapper import SPARQLWrapper, JSON, SPARQLExceptions
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from wikidata_discover.config import SPARQL_ENDPOINT, USER_AGENT

logger = logging.getLogger(__name__)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((
        SPARQLExceptions.EndPointInternalError,
        SPARQLExceptions.EndPointNotFound,
        Exception,
    )),
    before_sleep=lambda rs: logger.warning(
        "SPARQL retry #%d after %s", rs.attempt_number, rs.outcome.exception()
    ),
)
def execute_sparql_bindings(query: str) -> list[dict]:
    """
    Run any SPARQL query and return the full list of result bindings
    (the raw JSON objects) so callers can pull out whatever fields they need.
    """
    wrapper = SPARQLWrapper(SPARQL_ENDPOINT, agent=USER_AGENT)
    wrapper.setQuery(query)
    wrapper.setReturnFormat(JSON)
    resp = wrapper.query().convert()
    return resp["results"]["bindings"]


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
