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
