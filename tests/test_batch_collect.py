"""Batch runner helpers, importable without cloud access."""

import json
import os
import time

from wikidata_discover.batch import artifact_paths, load_done, parse_done


def test_artifact_paths_exact_names_and_freshness(tmp_path):
    results, run_dir = tmp_path, tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True); (results / "reports").mkdir(); (results / "cache").mkdir()
    (run_dir / "log.jsonl").write_text("")
    old = results / "missing_divisions_Q1.csv"; old.write_text("old")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    (results / "reports" / "Q1_report.json").write_text("{}")
    (results / "missing_divisions_Q12.csv").write_text("other qid")
    cache_used = results / "cache" / "abc.json"; cache_used.write_text("[]")
    os.utime(cache_used, (time.time() - 3600, time.time() - 3600))
    paths = {p.name for p in artifact_paths(results, run_dir, "Q1", since=time.time() - 60, extra=[cache_used])}
    assert "log.jsonl" in paths and "Q1_report.json" in paths and "abc.json" in paths
    assert "missing_divisions_Q1.csv" not in paths      # stale
    assert "missing_divisions_Q12.csv" not in paths     # different QID


def test_load_done_requires_ok_and_uploaded(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"qid": "Q1", "status": "ok", "uploaded": True},
        {"qid": "Q2", "status": "ok", "uploaded": False},
        {"qid": "Q3", "status": "failed", "uploaded": True},
    ]))
    assert load_done(log) == {"Q1"}


def test_parse_done_handles_garbage_lines():
    text = "not json\n" + json.dumps({"qid": "Q1", "status": "ok", "uploaded": True}) + "\n"
    assert parse_done(text) == {"Q1"}


def test_pick_qids_skips_done_and_duplicates():
    from wikidata_discover.cloud.collect_function import pick_qids
    rows = [["Q1", "a"], ["Q2", "b"], ["Q2", "b"], ["Q3", "c"], ["Q4", "d"]]
    assert pick_qids(rows, done={"Q2"}, limit=2) == ["Q1", "Q3"]
    assert pick_qids(rows, done={"Q1", "Q2", "Q3", "Q4"}, limit=5) == []
