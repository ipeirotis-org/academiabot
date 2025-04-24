#!/usr/bin/env python3
"""
Explore the organisational hierarchy under one university Q‑ID.

Traverses these predicates (both directions):

  downward:   P527  (has part)
              P355  (subsidiary)
              P199  (business division)
  upward  :   P361  (part of)
              P749  (parent organisation)

Outputs an indented tree and saves the edge list to hierarchy.json
"""

import sys, json, requests, time
from collections import defaultdict
from pathlib import Path

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS  = {
    "Accept": "application/sparql-results+json",
    "User-Agent": "Wikidata-Org-Hierarchy/0.3 (you@example.com)"
}

EDGE_PROPS = {
    "wdt:P527",       # has part
    "wdt:P355",       # subsidiary
    "wdt:P199",       # business division
    "wdt:P361",       # part of   (inverse in query)
    "wdt:P749"        # parent org (inverse in query)
}

SPARQL_TEMPLATE = """
SELECT DISTINCT ?parent ?parentLabel ?child ?childLabel WHERE {{
  VALUES ?root {{ wd:{root} }}

  # Accept any of the five predicates in either direction
  {{
    ?parent (wdt:P527|wdt:P355|wdt:P199) ?child .
  }}
  UNION
  {{
    ?child (wdt:P361|wdt:P749) ?parent .
  }}

  # Keep only edges that lie beneath the root
  ?root (wdt:P527|wdt:P355|wdt:P199|^wdt:P361|^wdt:P749)* ?parent .

  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""

def sparql_query(q):
    for _ in range(3):      # naïve retry
        r = requests.get(ENDPOINT, headers=HEADERS, params={"query": q}, timeout=60)
        if r.status_code == 200:
            return r.json()["results"]["bindings"]
        time.sleep(2)
    r.raise_for_status()    # raise final error

def get_edges(root_qid):
    q = SPARQL_TEMPLATE.format(root=root_qid)
    rows = sparql_query(q)
    edges = []
    for b in rows:
        parent = b["parent"]["value"].split("/")[-1]
        child  = b["child"]["value"].split("/")[-1]
        plabel = b["parentLabel"]["value"]
        clabel = b["childLabel"]["value"]
        edges.append((parent, plabel, child, clabel))
    return edges

def build_tree(root, edges):
    """Return adjacency dict root→[children] and label map."""
    children_of = defaultdict(list)
    label = {}
    label[root[0]] = root[1]
    for p, pl, c, cl in edges:
        children_of[p].append(c)
        label.setdefault(p, pl)
        label.setdefault(c, cl)
    return children_of, label

def print_tree(node, adj, label, depth=0, seen=None):
    if seen is None: seen=set()
    seen.add(node)
    prefix = "  " * depth + "└─ "
    print(f"{prefix}{label[node]} ({node})")
    for child in sorted(adj.get(node, []), key=lambda q: label[q]):
        if child not in seen:
            print_tree(child, adj, label, depth+1, seen)

def main():
    if len(sys.argv) != 2 or not sys.argv[1].startswith("Q"):
        sys.exit("Usage: python3 hierarchy.py Q13371")

    root_qid = sys.argv[1]

    # Fetch the root label
    lbl_row = sparql_query(
        f"SELECT ?l WHERE {{ wd:{root_qid} rdfs:label ?l FILTER(lang(?l)='en') }}")[0]
    root_label = lbl_row["l"]["value"]

    edges = get_edges(root_qid)
    Path("hierarchy.json").write_text(json.dumps(edges, indent=2))

    adj, label_map = build_tree((root_qid, root_label), edges)
    print_tree(root_qid, adj, label_map)
    print(f"\nTotal subordinate entities: {len(set(c for _,_,c,_ in edges))}")

if __name__ == "__main__":
    main()
