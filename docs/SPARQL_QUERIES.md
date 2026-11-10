# Useful SPARQL queries

Paste these into https://query.wikidata.org. The week 3 snapshot (`wikidata_discover/snapshot.py`)
runs the per-unit, parents, and qualifiers queries below. Until 2026-11-04 these queries were
section 11 of `TASKS.md`.

```sparql
# All children of a university (NYU here)
SELECT ?child ?childLabel ?childTypeLabel WHERE {
  ?child wdt:P749 wd:Q49210 .
  OPTIONAL { ?child wdt:P31 ?childType . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

```sparql
# Every unit below a university (NYU here), at any depth, through the same five
# properties the crawler follows. This is what a week 3 snapshot must contain.
SELECT DISTINCT ?unit ?unitLabel WHERE {
  wd:Q49210 (wdt:P527|wdt:P355|wdt:P199|^wdt:P361|^wdt:P749)+ ?unit .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

```sparql
# Departments with no parent organization (orphans). Both department classes we use.
SELECT ?dept ?deptLabel WHERE {
  VALUES ?deptClass { wd:Q1183543 wd:Q2467461 }
  ?dept wdt:P31 ?deptClass .
  FILTER NOT EXISTS { ?dept wdt:P749 ?parent }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

```sparql
# How many schools and departments each U.S. university has.
# Same university predicate as the harvester (subclasses of university included).
SELECT ?univ ?univLabel (COUNT(DISTINCT ?school) AS ?nSchool) (COUNT(DISTINCT ?dept) AS ?nDept)
WHERE {
  VALUES ?deptClass { wd:Q1183543 wd:Q2467461 }
  ?univ wdt:P31/wdt:P279* wd:Q3918 ; wdt:P17 wd:Q30 .
  OPTIONAL { ?school wdt:P749 ?univ ; wdt:P31 wd:Q31855 .
    OPTIONAL { ?dept wdt:P749 ?school ; wdt:P31 ?deptClass . }
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?univ ?univLabel
ORDER BY DESC(?nDept)
```

```sparql
# One unit's children, over full statements: every rank except deprecated, with the rank
# shown (the query above uses only best-rank statements and would miss a normal-rank second
# parent). The week 3 snapshot runs this on the university, then on each child it finds, and
# so on down. Keep the optimizer hint and the order inside each block: without them the query
# service times out on large universities. (Tested on NYU: 93 children in under a second.)
SELECT ?st ?prop ?child ?childLabel ?rank WHERE {
  hint:Query hint:optimizer "None" .
  { ?st ps:P749 wd:Q49210 . ?child p:P749 ?st . BIND("P749" AS ?prop) }
  UNION { ?st ps:P361 wd:Q49210 . ?child p:P361 ?st . BIND("P361" AS ?prop) }
  UNION { wd:Q49210 p:P527 ?st . ?st ps:P527 ?child . BIND("P527" AS ?prop) }
  UNION { wd:Q49210 p:P355 ?st . ?st ps:P355 ?child . BIND("P355" AS ?prop) }
  UNION { wd:Q49210 p:P199 ?st . ?st ps:P199 ?child . BIND("P199" AS ?prop) }
  ?st wikibase:rank ?rank . FILTER(?rank != wikibase:DeprecatedRank)
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

```sparql
# Every parent of the units found, over full statements: the crawl above goes downward only
# and misses a second parent outside the university's tree (NYU Shanghai, Q13652966, also has
# P749 to East China Normal University). Pass the units in VALUES, in batches. Keep the hint.
SELECT ?child ?prop ?parent ?parentLabel ?rank ?st WHERE {
  hint:Query hint:optimizer "None" .
  VALUES ?child { wd:Q13652966 wd:Q16256772 }
  { ?child p:P749 ?st . ?st ps:P749 ?parent . BIND("P749" AS ?prop) }
  UNION { ?child p:P361 ?st . ?st ps:P361 ?parent . BIND("P361" AS ?prop) }
  ?st wikibase:rank ?rank . FILTER(?rank != wikibase:DeprecatedRank)
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

```sparql
# The qualifiers on the statements the queries above returned (?st values, passed in a VALUES
# list; send it as a POST when the list is long). The snapshot keeps them with each link.
SELECT ?st ?qual ?value WHERE {
  VALUES ?st { <http://www.wikidata.org/entity/statement/...> }
  ?st ?qual ?value .
  FILTER(STRSTARTS(STR(?qual), "http://www.wikidata.org/prop/qualifier/P"))
}
```

```sparql
# Full-text search for an existing item, which finds labels that do not start with the
# name you typed ("Boston University Wheelock College..." for "Wheelock College").
SELECT ?item ?itemLabel ?itemDescription WHERE {
  SERVICE wikibase:mwapi {
    bd:serviceParam wikibase:endpoint "www.wikidata.org"; wikibase:api "Search";
                    mwapi:srsearch "Wheelock College Boston University"; mwapi:srlimit "10".
    ?item wikibase:apiOutputItem mwapi:title.
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```
