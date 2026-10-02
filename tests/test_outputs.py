"""Output files must land in the results directory, not the current working directory."""

from pathlib import Path

from wikidata_discover.config import RESULTS_DIR
from wikidata_discover.to_qs_wikidata import export_quickstatements, quickstatements_lines, PARENT_PROPERTY

SAMPLE = [{"name": "School of Testing", "unit_type": "school"}]


def test_export_writes_into_given_dir(tmp_path):
    out = export_quickstatements(SAMPLE, "Q1", "Test University", out_dir=tmp_path)
    assert out == tmp_path / "quickstatements_Q1.qs"
    assert out.exists()
    assert "School of Testing" in out.read_text()


def test_export_default_dir_is_results(monkeypatch, tmp_path):
    import wikidata_discover.to_qs_wikidata as mod
    monkeypatch.setattr(mod, "RESULTS_DIR", tmp_path / "results")
    out = export_quickstatements(SAMPLE, "Q2", "Test University")
    assert out.parent == tmp_path / "results"
    assert not Path("quickstatements_Q2.qs").exists()


def test_results_dir_is_inside_package():
    assert RESULTS_DIR.name == "results"
    assert RESULTS_DIR.parent.name == "wikidata_discover"


def test_orphans_are_linked_not_created():
    items = [
        {"name": "School of Testing", "unit_type": "school", "status": "missing"},
        {"name": "Existing School", "status": "orphan", "qid": "Q555"},
    ]
    lines = quickstatements_lines(items, "Q1", "Test University")
    assert lines.count("CREATE") == 1
    assert f"Q555|{PARENT_PROPERTY}|Q1" in lines
    assert not any("Existing School" in ln for ln in lines)


def test_orphan_without_qid_falls_back_to_create():
    lines = quickstatements_lines([{"name": "X", "status": "orphan"}], "Q1", "U")
    assert lines.count("CREATE") == 1


def test_unresolved_is_never_exported():
    items = [{"name": "Unknown Dept", "unit_type": "department", "status": "unresolved"}]
    assert quickstatements_lines(items, "Q1", "U") == []


def test_cap_applies_after_removing_unresolved():
    items = [{"name": "U", "status": "unresolved"}, {"name": "M", "unit_type": "school", "status": "missing"}]
    assert quickstatements_lines(items, "Q1", "U", max_items=1).count("CREATE") == 1
