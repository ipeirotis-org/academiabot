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
    # print(resp)
    return resp["results"]["bindings"]


def run_sparql(query: str) -> list[tuple[str, str]]:
    """
    Helper that returns (qid,label) tuples by picking out fields
    from the bindings. It now handles multiple expected key names.
    """
    bindings = execute_sparql_bindings(query)
    out: list[tuple[str, str]] = []

    for b in bindings:
        # Case 1: Handles 'child'/'childLabel' format
        if "child" in b and "childLabel" in b:
            qid = b["child"]["value"].rsplit("/", 1)[-1]
            label = b["childLabel"]["value"]
            out.append((qid, label))

        # Case 2 (FIX): Handles 'univ'/'univLabel' format from your data
        elif "univ" in b and "univLabel" in b:
            qid = b["univ"]["value"].rsplit("/", 1)[-1]
            label = b["univLabel"]["value"]
            out.append((qid, label))

        # Case 3 (Original else): Handles a simple 'label' format
        elif "label" in b:
            qid = ""
            label = b["label"]["value"]
            out.append((qid, label))
            
    return out