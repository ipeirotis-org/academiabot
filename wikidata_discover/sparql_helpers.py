from SPARQLWrapper import SPARQLWrapper, JSON
from wikidata_discover.config import SPARQL_ENDPOINT, USER_AGENT


def execute_sparql_bindings(query: str) -> list[dict]:
    """
    Run any SPARQL query and return the full list of result bindings
    (the raw JSON objects) so callers can pull out whatever fields they need.
    """
    wrapper = SPARQLWrapper(SPARQL_ENDPOINT, agent=USER_AGENT)
    wrapper.setQuery(query)
    wrapper.setReturnFormat(JSON)
    resp = wrapper.query().convert()
    # print(resp)
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
