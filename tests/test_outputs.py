"""Output files must land in the results directory, not the current working directory."""

from pathlib import Path

from wikidata_discover.config import RESULTS_DIR
from wikidata_discover.to_qs_wikidata import export_quickstatements

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
