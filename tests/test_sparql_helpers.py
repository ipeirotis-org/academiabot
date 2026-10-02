"""SPARQL helper: result parsing and tuple conversion, without network."""

from wikidata_discover import sparql_helpers as sh

PAYLOAD = {"results": {"bindings": [
    {"univ": {"value": "http://www.wikidata.org/entity/Q49210"}, "univLabel": {"value": "New York University"}},
    {"univ": {"value": "http://www.wikidata.org/entity/Q49088"}, "univLabel": {"value": "Columbia University"}},
]}}


def test_parse_bindings():
    assert len(sh._parse_bindings(PAYLOAD)) == 2


def test_run_sparql_as_tuples(monkeypatch):
    monkeypatch.setattr(sh, "execute_sparql_bindings", lambda q: sh._parse_bindings(PAYLOAD))
    assert sh.run_sparql("SELECT ...", as_tuples=True) == [("Q49210", "New York University"), ("Q49088", "Columbia University")]


def test_malformed_payload_raises():
    import pytest
    with pytest.raises(sh.SparqlBadResponse):
        sh._parse_bindings({"error": "something went wrong"})
    with pytest.raises(sh.SparqlBadResponse):
        sh._parse_bindings("not json at all")
    assert sh._parse_bindings({"results": {"bindings": []}}) == []


def test_retry_after_parsing():
    import time
    from email.utils import formatdate
    assert sh._retry_after_seconds("120") == 120
    assert sh._retry_after_seconds(None) == sh._RATE_LIMIT_WAIT
    assert sh._retry_after_seconds("garbage") == sh._RATE_LIMIT_WAIT
    future = formatdate(time.time() + 90, usegmt=True)
    assert 80 <= sh._retry_after_seconds(future) <= 91
