import logging
from collections import deque, defaultdict
from time import sleep
from typing import Dict, List, Tuple

from .sparql_helpers import execute_sparql_bindings
from .config import USER_AGENT

logger = logging.getLogger(__name__)

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
PREDICATES_UP = ["P361", "P749"]  # part of, parent org

# polite pause between SPARQL requests
time_sleep = 0.3

# One query for the whole subtree: everything reachable downward through
# "has part", "has subsidiary", "business division", or upward links pointing
# at the root ("part of", "parent organization"), to any depth.
DESCENDANTS_SPARQL = """
SELECT DISTINCT ?d WHERE {{
  wd:{root} (wdt:P527|wdt:P355|wdt:P199|^wdt:P361|^wdt:P749)+ ?d .
}}
"""


def descendant_qids(root_qid: str) -> set:
    """Return the QIDs of every descendant of root_qid.

    Uses a single property-path query (one request instead of three per node).
    If that query fails, for example a timeout on a very large institution,
    falls back to the node-by-node crawl in all_descendants().
    """
    try:
        rows = execute_sparql_bindings(DESCENDANTS_SPARQL.format(root=root_qid))
        return {b["d"]["value"].rsplit("/", 1)[-1] for b in rows}
    except Exception as e:  # noqa: BLE001 - any SPARQL failure means fall back
        logger.warning("descendant_qids: single query failed for %s (%s); crawling instead", root_qid, e)
        edges, _ = all_descendants(root_qid)
        return {child for _, child, _, _ in edges}


def all_descendants(
    root_qid: str,
) -> Tuple[List[Tuple[str, str, str, str]], Dict[str, str]]:
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
    # English label preferred, any language otherwise, the QID itself if none.
    label_q = (
        f"SELECT ?l WHERE {{ wd:{root_qid} rdfs:label ?l }} "
        f"ORDER BY DESC(LANG(?l) = 'en') LIMIT 1"
    )
    bindings = execute_sparql_bindings(label_q)
    labels[root_qid] = bindings[0]["l"]["value"] if bindings else root_qid

    while queue:
        parent = queue.popleft()
        for down, up in zip(PREDICATES_DOWN, PREDICATES_UP):
            query = SPARQL_TEMPLATE.format(parent=parent, down=down, up=up)
            rows = execute_sparql_bindings(query)
            for b in rows:
                child = b["child"]["value"].rsplit("/", 1)[-1]
                prop = b["propLabel"]["value"]
                ctype = b.get("childTypeLabel", {}).get("value", "—")
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
                edges.append((parent, child, prop, ctype))
                labels.setdefault(child, b["childLabel"]["value"])
            sleep(time_sleep)

    return edges, labels
