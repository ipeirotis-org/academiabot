from collections import deque, defaultdict
from time import sleep
from typing import Dict, List, Tuple

from sparql_helpers import execute_sparql_bindings
from config import USER_AGENT  # for logging or future use

# SPARQL template for crawling hierarchy
SPARQL_TEMPLATE = """
SELECT DISTINCT ?child ?childLabel ?propLabel ?childTypeLabel WHERE {{
  VALUES ?parent {{ wd:{parent} }}
  {{ ?parent wdt:{down} ?child . BIND(wdt:{down} AS ?prop) }}
  UNION
  {{ ?child wdt:{up} ?parent . BIND(wdt:{up} AS ?prop) }}
  OPTIONAL {{ ?child wdt:P31 ?childType . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""

# predicates for downward/upward traversal
PREDICATES_DOWN = ["P527", "P355", "P199"]  # has part, subsidiary, division
PREDICATES_UP = ["P361", "P749"]             # part of, parent org

# polite pause between SPARQL requests
time_sleep = 0.3


def all_descendants(root_qid: str) -> Tuple[List[Tuple[str, str, str, str]], Dict[str, str]]:
    """
    Crawl all parts and parent relations under a root entity via BFS.
    Returns:
      - edges: list of (parent_qid, child_qid, predicateLabel, childTypeLabel)
      - labels: map from qid to English label
    """
    queue = deque([root_qid])
    seen = {root_qid}
    edges: List[Tuple[str, str, str, str]] = []
    labels: Dict[str, str] = {}

    # fetch root label
    label_q = f"SELECT ?l WHERE {{ wd:{root_qid} rdfs:label ?l FILTER(lang(?l)='en') }}"
    binding = execute_sparql_bindings(label_q)[0]
    labels[root_qid] = binding["l"]["value"]

    while queue:
        parent = queue.popleft()
        for down, up in zip(PREDICATES_DOWN, PREDICATES_UP):
            query = SPARQL_TEMPLATE.format(parent=parent, down=down, up=up)
            rows = execute_sparql_bindings(query)
            for b in rows:
                child = b["child"]["value"].rsplit('/', 1)[-1]
                prop = b["propLabel"]["value"]
                ctype = b.get("childTypeLabel", {}).get("value", "—")
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
                edges.append((parent, child, prop, ctype))
                labels.setdefault(child, b["childLabel"]["value"])
            sleep(time_sleep)

    return edges, labels
