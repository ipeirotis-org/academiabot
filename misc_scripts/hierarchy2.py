#!/usr/bin/env python3
"""
BFS hierarchy crawler for a university Q‑ID.
Shows edge predicate + node type, avoids WDQS time‑outs
by issuing many small queries.
"""

import sys, json, time, requests
from pathlib import Path
from collections import deque, defaultdict

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS  = {
    "Accept": "application/sparql-results+json",
    "User-Agent": "Wikidata-Org-Hierarchy/0.3 (you@example.com)"
}

SLEEP = 0.3        # pause between queries – keep it polite

PREDICATES_DOWN = ["P527", "P355", "P199"]   # has part, subsidiary, division
PREDICATES_UP   = ["P361", "P749"]           # part of, parent org

SPARQL_TEMPLATE = """
SELECT DISTINCT ?child ?childLabel ?propLabel ?childTypeLabel WHERE {{
  VALUES ?parent {{ wd:{parent} }}
  {{
    ?parent wdt:{down} ?child .
    BIND(wdt:{down} AS ?prop)
  }}
  UNION
  {{
    ?child  wdt:{up}  ?parent .
    BIND(wdt:{up}  AS ?prop)
  }}
  OPTIONAL {{ ?child wdt:P31 ?childType . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""

def run(parent_qid, down, up):
    q = SPARQL_TEMPLATE.format(parent=parent_qid, down=down, up=up)
    r = requests.get(ENDPOINT, headers=HEADERS, params={"query": q}, timeout=60)
    r.raise_for_status()
    return r.json()["results"]["bindings"]

def bfs(root_qid):
    queue       = deque([root_qid])
    seen        = {root_qid}
    edges       = []            # (parent, child, propLabel, childType)
    labels      = {}            # Q‑ID → label

    # fetch root label once
    rlbl = requests.get(
        ENDPOINT, headers=HEADERS,
        params={"query": f"SELECT ?l WHERE {{ wd:{root_qid} rdfs:label ?l FILTER(lang(?l)='en') }}"}
    ).json()
    labels[root_qid] = rlbl["results"]["bindings"][0]["l"]["value"]

    while queue:
        parent = queue.popleft()
        for d, u in zip(PREDICATES_DOWN, PREDICATES_UP):
            rows = run(parent, d, u)
            time.sleep(SLEEP)
            for b in rows:
                child = b["child"]["value"].split("/")[-1]
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
                edges.append((
                    parent,
                    child,
                    b["propLabel"]["value"],
                    b.get("childTypeLabel", {}).get("value", "—")
                ))
                labels.setdefault(child, b["childLabel"]["value"])
    return edges, labels

def build_tree(root, edges):
    children = defaultdict(list)
    edge_lbl = {}
    ntype    = {}
    for p, c, prop, typ in edges:
        children[p].append(c)
        edge_lbl[(p, c)] = prop
        ntype.setdefault(c, typ)
    return children, edge_lbl, ntype

def print_tree(node, children, labels, edge_lbl, ntype, depth=0):
    t = ntype.get(node, "university" if depth == 0 else "—")
    print("  " * depth + f"└─ {labels[node]} ({node}) — type: {t}")
    for ch in sorted(children[node], key=lambda q: labels[q]):
        edge = edge_lbl[(node, ch)]
        print("  " * (depth+1) + f"[{edge}] ", end="")
        print_tree(ch, children, labels, edge_lbl, ntype, depth+1)

def main():
    if len(sys.argv) != 2 or not sys.argv[1].startswith("Q"):
        sys.exit("Usage: python3 bfs_hierarchy.py Q13371")
    root = sys.argv[1]

    edges, labels = bfs(root)
    Path("hierarchy.json").write_text(json.dumps(edges, indent=2))

    children, edge_lbl, ntype = build_tree(root, edges)
    print_tree(root, children, labels, edge_lbl, ntype)
    print(f"\nTotal subordinate entities: {len({c for _,c,_,_ in edges})}")

if __name__ == "__main__":
    main()
