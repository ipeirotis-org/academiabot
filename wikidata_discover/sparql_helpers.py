from SPARQLWrapper import SPARQLWrapper, JSON
from config import SPARQL_ENDPOINT, USER_AGENT
from typing import List, Tuple

def run_sparql(query: str) -> List[Tuple[str, str]]:
    wrapper = SPARQLWrapper(SPARQL_ENDPOINT, agent=USER_AGENT)
    wrapper.setQuery(query)
    wrapper.setReturnFormat(JSON)
    res = wrapper.query().convert()
    results = []
    for b in res["results"]["bindings"]:
        if "child" in b:
            qid = b["child"]["value"].split("/")[-1]
            label = b["childLabel"]["value"]
            results.append((qid, label))
        elif "label" in b:
            results.append(("", b["label"]["value"]))
    return results
