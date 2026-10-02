"""Batch runner helpers, importable without cloud access."""

import json
import os
import time

from wikidata_discover.scripts.batch_collect import artifact_paths, load_done


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
