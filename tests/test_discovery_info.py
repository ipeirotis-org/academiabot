"""University info lookup: English label preferred, any label as fallback, and the
label's language threaded into child lookups and searches. No network."""

import pytest
import wikidata_discover.discovery as disc


def _make(monkeypatch, bindings):
    monkeypatch.setattr(disc, "execute_sparql_bindings", lambda q: bindings)
    d = disc.Discovery.__new__(disc.Discovery)
    d.university_qid = "Q1"
    d.university_lang = "en"
    return d


def test_label_and_website(monkeypatch):
    d = _make(monkeypatch, [{"label": {"value": "Test University"}, "lang": {"value": "en"},
                             "website": {"value": "https://t.edu"}}])
    assert d.fetch_university_info() == ("Test University", "https://t.edu")
    assert d.university_lang == "en"


def test_website_optional_and_language_recorded(monkeypatch):
    d = _make(monkeypatch, [{"label": {"value": "Universidad de Prueba", "xml:lang": "es"}, "lang": {"value": "es"}}])
    assert d.fetch_university_info() == ("Universidad de Prueba", None)
    assert d.university_lang == "es"


def test_language_falls_back_to_xml_lang_then_en(monkeypatch):
    d = _make(monkeypatch, [{"label": {"value": "X", "xml:lang": "fr"}}])
    d.fetch_university_info(); assert d.university_lang == "fr"
    d = _make(monkeypatch, [{"label": {"value": "X"}, "lang": {"value": "not a language tag"}}])
    d.fetch_university_info(); assert d.university_lang == "en"


def test_no_label_at_all_raises(monkeypatch):
    d = _make(monkeypatch, [{"website": {"value": "https://t.edu"}}])
    with pytest.raises(ValueError):
        d.fetch_university_info()


def test_child_queries_use_university_language(monkeypatch):
    seen = []
    monkeypatch.setattr(disc, "run_sparql", lambda q, **k: seen.append(q) or [])
    monkeypatch.setattr(disc, "execute_sparql_bindings", lambda q: seen.append(q) or [])
    d = disc.Discovery.__new__(disc.Discovery)
    d.university_qid, d.university_lang = "Q1", "es"
    d.get_existing_children(); d.get_children_alt_labels()
    assert 'wikibase:language "en,es"' in seen[0] and "wd:Q1" in seen[0]
    assert 'IN ("en", "es")' in seen[1]


def test_search_adds_university_language_and_dedupes(monkeypatch):
    calls = []
    def fake_search(name, language="en"):
        calls.append(language)
        return [("Q9", "Facultad")] if language == "es" else [("Q9", "Faculty"), ("Q8", "Other")]
    monkeypatch.setattr(disc, "quick_wd_search", fake_search)
    d = disc.Discovery.__new__(disc.Discovery)
    d.university_qid, d.university_lang = "Q1", "es"
    assert d.search_wikidata("Faculty") == [("Q9", "Faculty"), ("Q8", "Other")]
    assert calls == ["en", "es"]
    d.university_lang = "en"; calls.clear()
    d.search_wikidata("Faculty"); assert calls == ["en"]
