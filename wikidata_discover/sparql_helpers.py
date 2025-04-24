from SPARQLWrapper import SPARQLWrapper, JSON
from config import SPARQL_ENDPOINT, USER_AGENT

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

def run_sparql(query: str) -> list[tuple[str,str]]:
    """
    Existing helper that returns only (qid,label) tuples by picking out
    'child'/'childLabel' or 'label' fields from the bindings.
    """
    bindings = execute_sparql_bindings(query)
    out: list[tuple[str,str]] = []
    for b in bindings:
        if "child" in b:
            qid   = b["child"]["value"].rsplit("/",1)[-1]
            label = b["childLabel"]["value"]
        else:
            qid   = ""
            label = b["label"]["value"]
        out.append((qid, label))
    return out
