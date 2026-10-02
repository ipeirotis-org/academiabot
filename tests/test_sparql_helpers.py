"""SPARQL helper: result parsing and tuple conversion, without network."""

from wikidata_discover import sparql_helpers as sh

PAYLOAD = {"results": {"bindings": [
    {"univ": {"value": "http://www.wikidata.org/entity/Q49210"}, "univLabel": {"value": "New York University"}},
    {"univ": {"value": "http://www.wikidata.org/entity/Q49088"}, "univLabel": {"value": "Columbia University"}},
]}}


def test_parse_bindings():
    assert len(sh._parse_bindings(PAYLOAD)) == 2
    assert sh._parse_bindings({}) == []


def test_run_sparql_as_tuples(monkeypatch):
    monkeypatch.setattr(sh, "execute_sparql_bindings", lambda q: sh._parse_bindings(PAYLOAD))
    assert sh.run_sparql("SELECT ...", as_tuples=True) == [("Q49210", "New York University"), ("Q49088", "Columbia University")]
