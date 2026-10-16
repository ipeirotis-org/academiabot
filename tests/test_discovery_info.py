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


def test_fallback_label_choice_is_deterministic():
    # several non-English labels must not tie: order by language, then label
    assert "ORDER BY DESC(BOUND(?en)) ?lang ?label" in disc.UNIV_INFO_SPARQL


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


def test_clear_exports_removes_only_this_universitys_files(tmp_path):
    for name in ("missing_divisions_Q1.csv", "quickstatements_Q1.qs", "missing_divisions_Q2.csv"):
        (tmp_path / name).write_text("old")
    assert disc.clear_exports("Q1", tmp_path) == 2
    assert not (tmp_path / "quickstatements_Q1.qs").exists() and (tmp_path / "missing_divisions_Q2.csv").exists()
    assert disc.clear_exports("Q1", tmp_path) == 0                   # nothing left, nothing fails


def test_discover_missing_leaves_an_unjudged_candidate_unresolved(monkeypatch, tmp_path):
    """Search works but no LLM can judge the match: unresolved, never missing, and the
    stale export files from an earlier run are gone."""
    import wikidata_discover.llm_helpers as lh
    monkeypatch.setattr(disc, "RESULTS_DIR", tmp_path)
    (tmp_path / "quickstatements_Q1.qs").write_text("stale")
    d = disc.Discovery.__new__(disc.Discovery)
    d.university_qid, d.university_label, d.university_website, d.university_lang = "Q1", "Test U", None, "en"
    monkeypatch.setattr(d, "get_existing_children", lambda: [("Q5", "School of Art")])
    monkeypatch.setattr(d, "get_all_descendants_qids", lambda: set())
    monkeypatch.setattr(d, "get_children_alt_labels", lambda: {})
    monkeypatch.setattr(d, "search_wikidata", lambda name: [("Q7", "Law School")])
    monkeypatch.setattr(disc.LLMHelper, "extract_divisions_best_available",
                        staticmethod(lambda u, w: [{"name": "School of Law"}]))
    def nobody(candidate, univ, children):
        raise lh.LLMUnavailable("no provider")
    monkeypatch.setattr(disc.LLMHelper, "choose_match", staticmethod(nobody))
    rows = d.discover_missing()
    assert [r["status"] for r in rows] == ["unresolved"]
    report = __import__("json").loads((tmp_path / "reports" / "Q1_report.json").read_text())
    assert report["unresolved"] == 1 and report["missing"] == 0
    assert report["unresolved_candidates"] == [{"name": "School of Law", "url": ""}]   # kept for a rerun
    assert not (tmp_path / "quickstatements_Q1.qs").exists()        # stale file removed, none written
    assert not (tmp_path / "missing_divisions_Q1.csv").exists()     # unresolved is not a CSV row either


def test_discover_missing_csv_holds_only_missing_and_orphans(monkeypatch, tmp_path):
    import pandas as pd
    monkeypatch.setattr(disc, "RESULTS_DIR", tmp_path)
    d = disc.Discovery.__new__(disc.Discovery)
    d.university_qid, d.university_label, d.university_website, d.university_lang = "Q1", "Test U", None, "en"
    monkeypatch.setattr(d, "get_existing_children", lambda: [])
    monkeypatch.setattr(d, "get_all_descendants_qids", lambda: set())
    monkeypatch.setattr(d, "get_children_alt_labels", lambda: {})
    def search(name):
        if name == "Broken": raise OSError("search down")
        return []
    monkeypatch.setattr(d, "search_wikidata", search)
    monkeypatch.setattr(disc.LLMHelper, "extract_divisions_best_available",
                        staticmethod(lambda u, w: [{"name": "New School"}, {"name": "Broken"}]))
    monkeypatch.setattr(disc.LLMHelper, "choose_match", staticmethod(lambda c, u, ch: None))
    d.discover_missing()
    csv = pd.read_csv(tmp_path / "missing_divisions_Q1.csv")
    assert list(csv["name"]) == ["New School"] and set(csv["status"]) == {"missing"}
    assert "Broken" not in (tmp_path / "quickstatements_Q1.qs").read_text()


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
