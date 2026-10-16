"""Descendant lookup: single query first, crawl as fallback. No network."""

from wikidata_discover import hierarchy as h

ROWS = [{"d": {"value": "http://www.wikidata.org/entity/Q1"}}, {"d": {"value": "http://www.wikidata.org/entity/Q2"}}]


def test_single_query_path(monkeypatch):
    calls = []
    monkeypatch.setattr(h, "execute_sparql_bindings", lambda q: calls.append(q) or ROWS)
    assert h.descendant_qids("Q49210") == {"Q1", "Q2"}
    assert len(calls) == 1
    assert "wd:Q49210" in calls[0] and "wdt:P749" in calls[0]


def test_falls_back_to_crawl_on_failure(monkeypatch):
    def boom(q):
        raise RuntimeError("timeout")
    monkeypatch.setattr(h, "execute_sparql_bindings", boom)
    monkeypatch.setattr(h, "all_descendants", lambda root: ([("Q49210", "Q9", "has part", "school")], {}))
    assert h.descendant_qids("Q49210") == {"Q9"}


def test_crawl_follows_every_relation_including_p199(monkeypatch):
    """The fallback crawl asks for all five relations in one query per node, so a
    unit linked only by P199 (business division) is found and crawled too."""
    def node(qid, label, prop):
        return {"child": {"value": f"http://www.wikidata.org/entity/{qid}"}, "childLabel": {"value": label},
                "propLabel": {"value": prop}}
    answers = {"Q99": [node("Q5", "Division", "business division")], "Q5": [node("Q6", "Lab", "parent organization")]}
    queries = []
    def fake(q):
        queries.append(q)
        if "rdfs:label" in q:
            return [{"l": {"value": "Root"}}]
        parent = q.split("VALUES ?parent { wd:")[1].split(" ")[0]
        return answers.get(parent, [])
    monkeypatch.setattr(h, "execute_sparql_bindings", fake)
    monkeypatch.setattr(h, "sleep", lambda s: None)
    edges, labels = h.all_descendants("Q99")
    assert [(p, c) for p, c, _, _ in edges] == [("Q99", "Q5"), ("Q5", "Q6")]
    crawl = [q for q in queries if "rdfs:label" not in q]
    assert len(crawl) == 3                                   # one query per node, not one per relation pair
    for prop in h.PREDICATES_DOWN + h.PREDICATES_UP:
        assert f"wdt:{prop}" in crawl[0]
    assert "?parent wdt:P199 ?child" in crawl[0] and "?child wdt:P749 ?parent" in crawl[0]


def test_crawl_tolerates_missing_root_label(monkeypatch):
    # First call is the root label lookup (empty), the rest are crawl steps (empty).
    monkeypatch.setattr(h, "execute_sparql_bindings", lambda q: [])
    monkeypatch.setattr(h, "sleep", lambda s: None)
    edges, labels = h.all_descendants("Q99")
    assert edges == [] and labels == {"Q99": "Q99"}
