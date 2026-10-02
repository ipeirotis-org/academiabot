"""University info lookup: English label preferred, any label as fallback. No network."""

import pytest
import wikidata_discover.discovery as disc


def _make(monkeypatch, bindings):
    monkeypatch.setattr(disc, "execute_sparql_bindings", lambda q: bindings)
    d = disc.Discovery.__new__(disc.Discovery)
    d.university_qid = "Q1"
    return d


def test_label_and_website(monkeypatch):
    d = _make(monkeypatch, [{"label": {"value": "Test University"}, "website": {"value": "https://t.edu"}}])
    assert d.fetch_university_info() == ("Test University", "https://t.edu")


def test_website_optional(monkeypatch):
    d = _make(monkeypatch, [{"label": {"value": "Universidad de Prueba", "xml:lang": "es"}}])
    assert d.fetch_university_info() == ("Universidad de Prueba", None)


def test_no_label_at_all_raises(monkeypatch):
    d = _make(monkeypatch, [{"website": {"value": "https://t.edu"}}])
    with pytest.raises(ValueError):
        d.fetch_university_info()
