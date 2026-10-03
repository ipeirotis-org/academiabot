"""Batch runner helpers and run_batch itself, against a fake bucket. No network."""

import json
import os
import time
from pathlib import Path

import pytest

import wikidata_discover.batch as batch
from wikidata_discover.batch import artifact_paths, load_done, object_name, parse_done


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


def test_parse_done_last_record_wins():
    recs = [{"qid": "Q1", "status": "ok", "uploaded": True},
            {"qid": "Q1", "status": "ok", "uploaded": False},   # run log upload failed later
            {"qid": "Q2", "status": "failed"},
            {"qid": "Q2", "status": "ok", "uploaded": True}]    # retried successfully
    assert parse_done("\n".join(json.dumps(r) for r in recs)) == {"Q2"}


def test_parse_done_keeps_unresolved_pending_until_max_attempts():
    attempt = {"qid": "Q1", "started": "t", "status": "ok", "uploaded": True, "unresolved_rows": 2}
    clean = {"qid": "Q2", "started": "t", "status": "ok", "uploaded": True, "unresolved_rows": 0}
    one = "\n".join(json.dumps(r) for r in [attempt, clean])
    assert parse_done(one) == {"Q2"}                              # Q1 rechecked next time
    three = "\n".join(json.dumps(r) for r in [attempt, attempt, attempt, clean])
    assert parse_done(three) == {"Q1", "Q2"}                      # left for a person after 3 tries
    assert parse_done(three, max_attempts=5) == {"Q2"}
    fixed = "\n".join(json.dumps(r) for r in [attempt, {**attempt, "unresolved_rows": 0}])
    assert parse_done(fixed) == {"Q1"}                            # second try resolved them


def test_parse_done_gives_up_on_repeated_failures():
    failed = {"qid": "Q1", "started": "t", "status": "failed", "error": "ValueError: no units"}
    assert parse_done("\n".join(json.dumps(r) for r in [failed, failed])) == set()       # retry
    assert parse_done("\n".join(json.dumps(r) for r in [failed, failed, failed])) == {"Q1"}  # leave it
    three = "\n".join(json.dumps(r) for r in [failed, failed, failed,
                                              {"qid": "Q2", "started": "t", "status": "ok", "uploaded": True}])
    assert batch.parse_states(three) == {"Q1": "exhausted", "Q2": "done"}
    assert batch.parse_exhausted(three) == {"Q1"}                                        # distinct from done


def test_summary_counts_universities_that_need_a_person(stub):
    failed = {"qid": "QFAIL", "started": "t", "status": "failed", "error": "x"}
    log = "\n".join(json.dumps(failed) for _ in range(3)) + "\n"
    bucket = FakeBucket({"runs/r23/log.jsonl": log, "runs/r23/run.json": "{}"})
    s = batch.run_batch("r23", ["QFAIL"], bucket, results_dir=stub, report=lambda m: None)
    assert s["skipped_done"] == 1 and s["needs_review"] == 1
    end = json.loads(bucket.store["runs/r23/invocations.jsonl"].splitlines()[-1])
    assert end["summary"]["needs_review"] == 1
    # this invocation makes the third failed attempt: the count is recomputed at the end
    log2 = "\n".join(json.dumps(failed) for _ in range(2)) + "\n"
    bucket = FakeBucket({"runs/r24/log.jsonl": log2, "runs/r24/run.json": "{}"})
    s = batch.run_batch("r24", ["QFAIL"], bucket, results_dir=stub, report=lambda m: None)
    assert s["processed"] == 1 and s["needs_review"] == 1
    end = json.loads(bucket.store["runs/r24/invocations.jsonl"].splitlines()[-1])
    assert end["summary"]["needs_review"] == 1


def test_invocation_id_ties_records_together(stub):
    bucket = FakeBucket()
    batch.run_batch("r25", ["Q1"], bucket, results_dir=stub, report=lambda m: None)
    start, end = [json.loads(l) for l in bucket.store["runs/r25/invocations.jsonl"].splitlines()]
    qid_rec = json.loads(bucket.store["runs/r25/log.jsonl"].splitlines()[0])
    assert start["invocation_id"] == end["invocation_id"] == qid_rec["invocation_id"]
    assert len(start["invocation_id"]) == 12
    batch.run_batch("r25", ["Q2"], bucket, results_dir=stub, report=lambda m: None)
    ids = {json.loads(l)["invocation_id"] for l in bucket.store["runs/r25/invocations.jsonl"].splitlines()}
    assert len(ids) == 2                                              # a second invocation gets its own id


def test_run_id_must_be_a_safe_path_component(stub):
    import wikidata_discover.cloud.collect_function as cf
    for bad in ("..", ".", "../x", "a/b", "/abs", ".hidden", "", "x" * 101, None):
        with pytest.raises(ValueError):
            batch.validate_run_id(bad)
    assert batch.validate_run_id("cloud-2026-10-02_v1.2") == "cloud-2026-10-02_v1.2"
    with pytest.raises(ValueError):
        batch.run_batch("../escape", ["Q1"], FakeBucket(), results_dir=stub, report=lambda m: None)
    with pytest.raises(ValueError):
        cf.parse_request({"run_id": "../escape"})
    with pytest.raises(ValueError):
        cf.parse_request({"list_object": "../other-bucket-path.json"})


def test_empty_llm_answers_raise_instead_of_an_empty_report(monkeypatch):
    from wikidata_discover.llm_helpers import LLMHelper
    for name in ("extract_divisions_openai", "extract_divisions_anthropic", "extract_divisions_gemini"):
        monkeypatch.setattr(LLMHelper, name, staticmethod(lambda label, website: []))
    with pytest.raises(ValueError):
        LLMHelper.extract_divisions_best_available("Not a University", None)


def test_collect_treats_empty_qids_as_explicit(monkeypatch):
    import flask
    import wikidata_discover.cloud.collect_function as cf
    calls = {}
    monkeypatch.setattr(cf, "load_keys_from_secret_manager", lambda: None)
    monkeypatch.setattr(cf, "ensure_user_agent", lambda: None)
    monkeypatch.setattr(cf, "run_batch", lambda run_id, qids, bucket, **kw: calls.update(qids=list(qids), **kw) or
                        {"run_id": run_id, "failed": 0})

    class NeverBucket(FakeBucket):
        def blob(self, name):
            if name.startswith("runs/"):           # the run log may be read
                return super().blob(name)
            raise AssertionError(f"the university list must not be read, got {name}")
    class FakeClient:
        def __init__(self, project=None): pass
        def bucket(self, name): return NeverBucket()
    import google.cloud.storage as gcs
    monkeypatch.setattr(gcs, "Client", FakeClient)

    class Req:
        def get_json(self, silent=True): return {"qids": [], "run_id": "r9"}
    with flask.Flask(__name__).app_context():
        resp, status = cf.collect(Req())
    assert status == 200 and calls["qids"] == [] and calls["invocation_args"]["explicit_qids"] is True


def test_collect_records_invocation_when_list_is_exhausted(monkeypatch):
    import flask
    import wikidata_discover.cloud.collect_function as cf
    calls = {}
    monkeypatch.setattr(cf, "load_keys_from_secret_manager", lambda: None)
    monkeypatch.setattr(cf, "ensure_user_agent", lambda: None)
    monkeypatch.setattr(cf, "run_batch", lambda run_id, qids, bucket, **kw: calls.update(qids=list(qids), **kw) or
                        {"run_id": run_id, "failed": 0})
    store = {"universities_us.json": json.dumps([["Q1", "a"]]),
             "runs/r11/log.jsonl": json.dumps({"qid": "Q1", "started": "t", "status": "ok", "uploaded": True}) + "\n"}
    class FakeClient:
        def __init__(self, project=None): pass
        def bucket(self, name): return FakeBucket(store)
    import google.cloud.storage as gcs
    monkeypatch.setattr(gcs, "Client", FakeClient)

    class Req:
        def get_json(self, silent=True): return {"run_id": "r11"}
    with flask.Flask(__name__).app_context():
        resp, status = cf.collect(Req())
    assert calls["qids"] == [] and calls["invocation_args"]["list_done"] == 1
    assert resp.get_json()["message"] == "nothing left to do"
    # The preflight reads (run log, university list) were bounded by the kill time,
    # which is set at handler entry and cleared on the way out.
    import wikidata_discover.config as config
    reads = {(op, name.rsplit("/", 1)[-1]) for op, name, t in store["__calls__"]}
    assert ("exists", "log.jsonl") in reads and ("download", "universities_us.json") in reads
    assert all(5 <= t <= 60 for op, name, t in store["__calls__"])
    assert config.HARD_DEADLINE is None and config.DEADLINE is None


def test_collect_treats_malformed_json_as_a_bad_request(monkeypatch):
    """Truncated JSON must not pass for an empty body and start the default run."""
    import flask
    import wikidata_discover.cloud.collect_function as cf
    calls = {}
    monkeypatch.setattr(cf, "load_keys_from_secret_manager", lambda: None)
    monkeypatch.setattr(cf, "ensure_user_agent", lambda: None)
    monkeypatch.setattr(cf, "run_batch", lambda run_id, qids, bucket, **kw: calls.update(qids=list(qids), **kw) or
                        {"run_id": run_id, "failed": 1, "outcome": "failed"})
    class FakeClient:
        def __init__(self, project=None): pass
        def bucket(self, name): raise AssertionError("the bucket must not be touched")
    import google.cloud.storage as gcs
    monkeypatch.setattr(gcs, "Client", FakeClient)
    class Req:
        data = b'{"run_id": "r13", "qids": ['
        def get_json(self, silent=True): return None
    with flask.Flask(__name__).app_context():
        resp, status = cf.collect(Req())
    assert status == 207 and calls["qids"] == [] and "not valid JSON" in calls["fail_reason"]
    class Empty:
        data = b"   "
        def get_json(self, silent=True): return None
    monkeypatch.setattr(gcs, "Client", type("C", (), {"__init__": lambda self, project=None: None,
                                                        "bucket": lambda self, name: FakeBucket({"universities_us.json": "[]"})}))
    with flask.Flask(__name__).app_context():
        resp, status = cf.collect(Empty())
    assert calls.get("fail_reason") is None                                 # a blank body is still "no body"


def test_collect_refuses_preflight_reads_when_the_kill_is_imminent(monkeypatch):
    import flask
    import wikidata_discover.cloud.collect_function as cf
    calls = {}
    monkeypatch.setattr(cf, "load_keys_from_secret_manager", lambda: None)
    monkeypatch.setattr(cf, "ensure_user_agent", lambda: None)
    monkeypatch.setattr(cf, "FUNCTION_TIMEOUT_S", 5.0)          # as if entered with 5 s left
    monkeypatch.setattr(cf, "run_batch", lambda run_id, qids, bucket, **kw: calls.update(qids=list(qids), **kw) or
                        {"run_id": run_id, "failed": 1, "outcome": "failed"})
    store = {"universities_us.json": json.dumps([["Q1", "a"]])}
    class FakeClient:
        def __init__(self, project=None): pass
        def bucket(self, name): return FakeBucket(store)
    import google.cloud.storage as gcs
    monkeypatch.setattr(gcs, "Client", FakeClient)
    class Req:
        def get_json(self, silent=True): return {"run_id": "r12"}
    with flask.Flask(__name__).app_context():
        resp, status = cf.collect(Req())
    assert "DeadlineExceeded" in calls["fail_reason"] and calls["qids"] == []   # recorded, nothing started
    assert "__calls__" not in store                                            # no read even began


def test_collect_records_a_preflight_failure(monkeypatch):
    import flask
    import wikidata_discover.cloud.collect_function as cf
    calls = {}
    monkeypatch.setattr(cf, "load_keys_from_secret_manager", lambda: None)
    monkeypatch.setattr(cf, "ensure_user_agent", lambda: None)
    monkeypatch.setattr(cf, "run_batch", lambda run_id, qids, bucket, **kw: calls.update(qids=list(qids), **kw) or
                        {"run_id": run_id, "failed": 1, "outcome": "failed"})
    class DeadBlob:
        def exists(self, **kw): raise OSError("bucket unreachable")
    class DeadBucket:
        def blob(self, name): return DeadBlob()
    class FakeClient:
        def __init__(self, project=None): pass
        def bucket(self, name): return DeadBucket()
    import google.cloud.storage as gcs
    monkeypatch.setattr(gcs, "Client", FakeClient)

    class Req:
        headers = {}
        def get_json(self, silent=True): return {"run_id": "r17"}
    with flask.Flask(__name__).app_context():
        resp, status = cf.collect(Req())
    assert status == 207 and calls["qids"] == []                      # run_batch still records it
    assert calls["invocation_args"]["preflight_error"].startswith("OSError")
    assert calls["fail_reason"].startswith("OSError")                  # so its end record says failed
    assert resp.get_json()["outcome"] == "failed"


def test_collect_records_an_initialisation_failure(monkeypatch):
    import flask
    import wikidata_discover.cloud.collect_function as cf
    calls = {}
    def no_keys():
        raise RuntimeError("No LLM API key available")
    monkeypatch.setattr(cf, "load_keys_from_secret_manager", no_keys)
    monkeypatch.setattr(cf, "run_batch", lambda run_id, qids, bucket, **kw: calls.update(qids=list(qids), bucket=bucket, **kw) or
                        {"run_id": run_id, "failed": 1, "outcome": "failed"})
    class Req:
        headers = {}
        def get_json(self, silent=True): return {"run_id": "r20"}
    with flask.Flask(__name__).app_context():
        resp, status = cf.collect(Req())
    assert status == 207 and calls["qids"] == []
    assert calls["fail_reason"].startswith("init: RuntimeError: No LLM API key")
    assert isinstance(calls["bucket"], cf.UnreachableBucket)            # run_batch can still record locally


def test_collect_records_a_bad_request(monkeypatch):
    import flask
    import wikidata_discover.cloud.collect_function as cf
    calls = {}
    monkeypatch.setattr(cf, "load_keys_from_secret_manager", lambda: None)
    monkeypatch.setattr(cf, "ensure_user_agent", lambda: None)
    monkeypatch.setattr(cf, "run_batch", lambda run_id, qids, bucket, **kw: calls.update(qids=list(qids), **kw) or
                        {"run_id": run_id, "failed": 1, "outcome": "failed"})
    class Req:
        headers = {}
        def get_json(self, silent=True): return {"run_id": "r21", "max_universities": "sixty"}
    with flask.Flask(__name__).app_context():
        resp, status = cf.collect(Req())
    assert status == 207 and calls["qids"] == []
    assert calls["fail_reason"].startswith("bad request: max_universities must be a number")
    assert 1790 < calls["hard_deadline_s"] <= 1800                 # measured from handler entry


def test_parse_request_rejects_non_objects_and_bad_lists():
    import wikidata_discover.cloud.collect_function as cf
    with pytest.raises(ValueError):
        cf.parse_request(["Q1"])
    with pytest.raises(ValueError):
        cf.parse_request({"qids": "Q1"})
    p = cf.parse_request({"qids": None, "time_budget_s": "900"})
    assert p["qids"] == [] and p["time_budget_s"] == 900.0 and p["max_universities"] == 60


def test_parse_request_rejects_non_finite_and_negative_numbers():
    import wikidata_discover.cloud.collect_function as cf
    for bad in ({"reserve_s": "nan"}, {"reserve_s": float("nan")}, {"time_budget_s": "inf"},
                {"time_budget_s": -5}, {"max_universities": -1}, {"max_universities": float("inf")}):
        with pytest.raises(ValueError):
            cf.parse_request(bad)
    assert cf.parse_request({"reserve_s": 0, "max_universities": 0})["reserve_s"] == 0.0


def test_resume_reads_are_bounded_like_writes(stub):
    prior_log = json.dumps({"qid": "Q1", "status": "ok", "uploaded": True, "cache_files": ["c.json"]}) + "\n"
    bucket = FakeBucket({"runs/r34/log.jsonl": prior_log, "runs/r34/invocations.jsonl": "{}\n",
                         "runs/r34/run.json": "{}", "runs/r34/cache/c.json": "[]", "runs/r34/cache/orphan.json": "[]"})
    batch.run_batch("r34", ["Q1", "Q2"], bucket, results_dir=stub, report=lambda m: None, hard_deadline_s=1800)
    ops = {op for op, name, t in bucket.store["__calls__"]}
    assert {"exists", "download", "list", "upload"} <= ops                  # every read carried a timeout
    assert all(5 <= t <= 60 for op, name, t in bucket.store["__calls__"])
    assert (stub / "cache" / "orphan.json").exists()                        # the restore itself still works
    # Almost no platform time at entry: the first read is refused, the cold start is
    # recorded as refused instead of running past the kill.
    bucket = FakeBucket({"runs/r35/log.jsonl": prior_log})
    s = batch.run_batch("r35", ["Q1"], bucket, results_dir=stub, report=lambda m: None, hard_deadline_s=8)
    assert s["outcome"] == "failed" and s["processed"] == 0 and "DeadlineExceeded" in s["sync_error"]
    assert (stub / "runs" / "r35" / "refused.jsonl").exists()


def test_caller_identity_from_scheduler_header_or_verified_token():
    import base64, json
    import wikidata_discover.cloud.collect_function as cf
    def bearer(email):
        payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
        return f"Bearer aaa.{payload}.sig"
    job = {"X-CloudScheduler-JobName": "academiabot-collect-slice"}
    # the job header counts only with the scheduler account's own verified token
    assert cf.caller_identity({**job, "Authorization": bearer(cf.SCHEDULER_SA)}) == "cloud-scheduler:academiabot-collect-slice"
    assert cf.caller_identity({**job, "Authorization": bearer("panos@example.org")}) == "manual:panos@example.org"
    assert cf.caller_identity(job) == "manual:unknown caller"
    assert cf.caller_identity({"Authorization": bearer("panos@example.org")}) == "manual:panos@example.org"
    assert cf.caller_identity({"Authorization": "Bearer not-a-jwt"}) == "manual:unknown caller"
    assert cf.caller_identity({"X-Goog-Authenticated-User-Email": "forged@example.org"}) == "manual:unknown caller"
    # with both headers Cloud Run verified only X-Serverless-Authorization: Authorization is ignored
    both = {"X-Serverless-Authorization": bearer("real@example.org"), "Authorization": bearer(cf.SCHEDULER_SA), **job}
    assert cf.caller_identity(both) == "manual:real@example.org"


def test_uploads_are_bounded_by_the_deadline(stub, monkeypatch):
    import wikidata_discover.config as config
    bucket = FakeBucket()
    batch.run_batch("r26", ["Q1"], bucket, results_dir=stub, report=lambda m: None, hard_deadline_s=1800)
    assert all(t is not None and t <= 60 for t in bucket.store["__timeouts__"])     # every upload has a timeout
    import wikidata_discover.discovery as disc
    # Work deadline passed, kill 90 s away: the shutdown buffer is for exactly these
    # uploads, so the attempt's records and artifacts still reach the bucket, bounded.
    class Slow:
        def __init__(self, qid): self.university_qid, self.university_label = qid, qid
        def discover_missing(self):
            config.DEADLINE = time.time() - 1; config.HARD_DEADLINE = time.time() + 89; return []
    monkeypatch.setattr(disc, "Discovery", Slow)
    bucket = FakeBucket()
    s = batch.run_batch("r27", ["Q1"], bucket, results_dir=stub, report=lambda m: None, hard_deadline_s=1800)
    rec = json.loads((stub / "runs" / "r27" / "log.jsonl").read_text().splitlines()[0])
    assert rec["uploaded"] is True and s["outcome"] == "ok"
    assert "runs/r27/invocations.jsonl" in bucket.store
    assert all(5 <= t <= 60 for t in bucket.store["__timeouts__"])
    # Kill already passed: no upload starts, the QID is not marked uploaded, the run is failed.
    class Stuck(Slow):
        def discover_missing(self):
            config.DEADLINE = config.HARD_DEADLINE = time.time() - 1; return []
    monkeypatch.setattr(disc, "Discovery", Stuck)
    bucket = FakeBucket()
    s = batch.run_batch("r28", ["Q1"], bucket, results_dir=stub, report=lambda m: None, hard_deadline_s=1800)
    rec = json.loads((stub / "runs" / "r28" / "log.jsonl").read_text().splitlines()[0])
    assert rec["uploaded"] is False and s["outcome"] == "failed"
    assert "runs/r28/reports/Q1_report.json" not in bucket.store
    assert config.HARD_DEADLINE is None and config.DEADLINE is None                 # cleared afterwards


def test_upload_timeout_tracks_the_kill_time(monkeypatch):
    import wikidata_discover.config as config
    from wikidata_discover.sparql_helpers import DeadlineExceeded
    monkeypatch.setattr(config, "HARD_DEADLINE", None)
    assert batch.upload_timeout() == 60
    monkeypatch.setattr(config, "HARD_DEADLINE", time.time() + 40)
    assert 25 <= batch.upload_timeout() <= 30                 # 40 s left minus the 10 s margin
    monkeypatch.setattr(config, "HARD_DEADLINE", time.time() + 8)
    with pytest.raises(DeadlineExceeded):
        batch.upload_timeout()


def test_resume_keeps_the_original_run_json_through_a_warm_outage(stub):
    original = json.dumps({"run_id": "r29", "operator": "first", "started": "earlier"})
    prior_log = json.dumps({"qid": "Q1", "status": "ok", "uploaded": True}) + "\n"
    bucket = FakeBucket({"runs/r29/run.json": original, "runs/r29/log.jsonl": prior_log,
                         "runs/r29/invocations.jsonl": "{}\n"})
    batch.run_batch("r29", ["Q1", "Q2"], bucket, results_dir=stub, report=lambda m: None)
    assert (stub / "runs" / "r29" / "run.json").read_text() == original   # came down with the histories
    # Same instance, bucket gone: the run continues and no second run.json is minted.
    class DeadBlob(FakeBlob):
        def exists(self, **kw): raise OSError("bucket unreachable")
        def upload_from_filename(self, path, **kw): raise OSError("bucket unreachable")
    class DeadBucket(FakeBucket):
        def blob(self, name): return DeadBlob(self.store, name)
    s = batch.run_batch("r29", ["Q1", "Q2", "Q3"], DeadBucket(), results_dir=stub, report=lambda m: None)
    assert s["processed"] >= 1 and (stub / "runs" / "r29" / "run.json").read_text() == original
    # Bucket back: the upload carries the original, not a rewrite.
    batch.run_batch("r29", ["Q3"], bucket, results_dir=stub, report=lambda m: None)
    assert bucket.store["runs/r29/run.json"] == original


def test_batch_collect_refuses_a_bad_run_id_before_touching_disk(monkeypatch, tmp_path):
    import wikidata_discover.scripts.batch_collect as bc
    monkeypatch.setattr(bc, "RESULTS_DIR", tmp_path)
    assert bc.main(["../escape", "Q1"]) == 2
    assert not (tmp_path.parent / "escape").exists() and not list(tmp_path.iterdir())


def test_batch_collect_records_an_initialisation_failure(monkeypatch, tmp_path):
    import wikidata_discover.scripts.batch_collect as bc
    calls = {}
    def no_keys():
        raise RuntimeError("No LLM API key available")
    monkeypatch.setattr(bc, "load_keys_from_secret_manager", no_keys)
    monkeypatch.setattr(bc, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(bc, "run_batch", lambda run_id, qids, bucket, **kw: calls.update(qids=list(qids), bucket=bucket, **kw) or
                        {"failed": 1})
    assert bc.main(["r22", "Q1"]) == 1
    assert calls["fail_reason"].startswith("init: RuntimeError") and isinstance(calls["bucket"], bc.UnreachableBucket)


def test_collect_explicit_list_skips_done_before_the_cap(monkeypatch):
    import flask
    import wikidata_discover.cloud.collect_function as cf
    calls = {}
    monkeypatch.setattr(cf, "load_keys_from_secret_manager", lambda: None)
    monkeypatch.setattr(cf, "ensure_user_agent", lambda: None)
    monkeypatch.setattr(cf, "run_batch", lambda run_id, qids, bucket, **kw: calls.update(qids=list(qids)) or
                        {"run_id": run_id, "failed": 0})
    done_log = "".join(json.dumps({"qid": q, "started": "t", "status": "ok", "uploaded": True}) + "\n" for q in ("Q1", "Q2"))
    store = {"runs/r13/log.jsonl": done_log}
    class FakeClient:
        def __init__(self, project=None): pass
        def bucket(self, name): return FakeBucket(store)
    import google.cloud.storage as gcs
    monkeypatch.setattr(gcs, "Client", FakeClient)

    class Req:
        def get_json(self, silent=True): return {"run_id": "r13", "qids": ["Q1", "Q2", "Q3", "Q4"], "max_universities": 2}
    with flask.Flask(__name__).app_context():
        cf.collect(Req())
    assert calls["qids"] == ["Q3", "Q4"]   # not ["Q1", "Q2"] again


def test_object_name_never_doubles_run_prefix(tmp_path):
    results, run_dir = tmp_path, tmp_path / "runs" / "r1"
    assert object_name("r1", run_dir / "log.jsonl", results, run_dir) == "runs/r1/log.jsonl"
    assert object_name("r1", results / "reports" / "Q1_report.json", results, run_dir) == "runs/r1/reports/Q1_report.json"
    assert object_name("r1", results / "missing_divisions_Q1.csv", results, run_dir) == "runs/r1/missing_divisions_Q1.csv"


def test_pick_qids_skips_done_and_duplicates():
    from wikidata_discover.cloud.collect_function import pick_qids
    rows = [["Q1", "a"], ["Q2", "b"], ["Q2", "b"], ["Q3", "c"], ["Q4", "d"]]
    assert pick_qids(rows, done={"Q2"}, limit=2) == ["Q1", "Q3"]
    assert pick_qids(rows, done={"Q1", "Q2", "Q3", "Q4"}, limit=5) == []
    assert pick_qids(rows, done=set(), limit=0) == []
    assert pick_qids(rows, done=set(), limit=-3) == []


def test_pick_qids_accepts_every_list_shape():
    from wikidata_discover.cloud.collect_function import pick_qids, row_qid
    binding = {"university": {"type": "uri", "value": "http://www.wikidata.org/entity/Q5"},
               "universityLabel": {"type": "literal", "value": "Five U"}}
    assert row_qid(binding) == "Q5"
    assert row_qid({"univ": {"value": "http://www.wikidata.org/entity/Q6"}}) == "Q6"
    assert pick_qids([binding, "Q7", ("Q8", "Eight"), {}], done=set(), limit=10) == ["Q5", "Q7", "Q8"]


def test_secret_manager_keys_reach_config(monkeypatch):
    from wikidata_discover import config
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", None)

    class Payload:
        def __init__(self, d): self.data = d
    class Resp:
        def __init__(self, d): self.payload = Payload(d)
    class FakeSM:
        def access_secret_version(self, request, **kw):
            return Resp(f"secret-for-{request['name'].split('/')[3]}\n".encode())

    batch.load_keys_from_secret_manager(client=FakeSM())
    assert config.OPENAI_API_KEY == "sk-from-env"                     # env wins, config updated
    assert config.ANTHROPIC_API_KEY == "secret-for-anthropic-api-key"  # stripped
    assert os.environ["GOOGLE_API_KEY"] == "secret-for-gemini-api-key"


def test_missing_optional_secret_is_tolerated(monkeypatch):
    from wikidata_discover import config
    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False); monkeypatch.setattr(config, env, None)
    monkeypatch.setattr(batch, "_injected", {})

    class Payload:
        def __init__(self, d): self.data = d
    class Resp:
        def __init__(self, d): self.payload = Payload(d)
    class OnlyOpenAI:
        def access_secret_version(self, request, **kw):
            if "openai" in request["name"]:
                return Resp(b"sk-only")
            raise PermissionError("denied")

    assert batch.load_keys_from_secret_manager(client=OnlyOpenAI()) == {
        "OPENAI_API_KEY": "secret_manager", "ANTHROPIC_API_KEY": "missing", "GOOGLE_API_KEY": "missing"}
    assert config.OPENAI_API_KEY == "sk-only" and config.ANTHROPIC_API_KEY is None

    class Nothing:
        def access_secret_version(self, request, **kw):
            raise PermissionError("denied")
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)   # the first call exported it
    monkeypatch.setattr(batch, "_injected", {})           # and remembered it as last good value
    with pytest.raises(RuntimeError):
        batch.load_keys_from_secret_manager(client=Nothing())


def test_ensure_user_agent_updates_config_after_import(monkeypatch):
    from wikidata_discover import config, sparql_helpers, wikidata_api
    monkeypatch.delenv("WD_BOT_USERAGENT", raising=False)
    monkeypatch.setattr(config, "USER_AGENT", "AcademiaBot/1.0 (ipeirotis@example.com)")
    assert batch.ensure_user_agent("Bot/1 (real@example.org)") == "Bot/1 (real@example.org)"
    assert config.USER_AGENT == "Bot/1 (real@example.org)" == os.environ["WD_BOT_USERAGENT"]
    # the request modules read it at call time, not at import
    seen = {}
    monkeypatch.setattr(wikidata_api.time, "sleep", lambda s: None)
    class R:
        status_code = 200; headers = {}
        def raise_for_status(self): pass
        def json(self): return {"search": []}
    monkeypatch.setattr(wikidata_api.requests, "get", lambda *a, **k: seen.update(k["headers"]) or R())
    wikidata_api.quick_wd_search("x")
    assert seen["User-Agent"] == "Bot/1 (real@example.org)"
    monkeypatch.setattr(sparql_helpers.requests, "get", lambda *a, **k: seen.update(k["headers"]) or R())
    monkeypatch.setattr(sparql_helpers.time, "sleep", lambda s: None)
    sparql_helpers._get("SELECT 1")
    assert seen["User-Agent"] == "Bot/1 (real@example.org)"
    monkeypatch.setenv("WD_BOT_USERAGENT", "FromEnv/1")
    assert batch.ensure_user_agent("ignored") == "FromEnv/1"


def test_llm_clients_read_keys_at_call_time(monkeypatch):
    from wikidata_discover import config, llm_helpers
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-late")
    monkeypatch.setattr(llm_helpers, "_openai_client", None)
    assert llm_helpers._get_openai_client().api_key == "sk-late"


# ---- run_batch against a fake bucket -------------------------------------------------

class FakeBlob:
    def __init__(self, store, name, fail_on=()):
        self.store, self.name, self.fail_on = store, name, fail_on
    def _note(self, op, timeout):
        if isinstance(self.store.get("__timeouts__", []), list):
            self.store.setdefault("__timeouts__", []).append(timeout)
            self.store.setdefault("__calls__", []).append((op, self.name, timeout))
    def exists(self, timeout=None, **kw):
        if timeout is not None:
            self._note("exists", timeout)
        return self.name in self.store
    def download_as_text(self, timeout=None, **kw):
        if timeout is not None:
            self._note("download", timeout)
        return self.store[self.name]
    def upload_from_filename(self, path, timeout=None, **kw):
        if any(self.name.endswith(s) for s in self.fail_on):
            raise OSError(f"simulated upload failure for {self.name}")
        self.store[self.name] = Path(path).read_text()
        self._note("upload", timeout)
    def delete(self, timeout=None, **kw):
        self._note("delete", timeout)
        del self.store[self.name]


class FakeBucket:
    def __init__(self, store=None, fail_on=()):
        self.store = {} if store is None else store
        self.fail_on = fail_on
    def blob(self, name):
        return FakeBlob(self.store, name, self.fail_on)
    def list_blobs(self, prefix="", timeout=None, **kw):
        if timeout is not None and isinstance(self.store.get("__timeouts__", []), list):
            self.store.setdefault("__timeouts__", []).append(timeout)
            self.store.setdefault("__calls__", []).append(("list", prefix, timeout))
        return [FakeBlob(self.store, n, self.fail_on) for n in sorted(self.store) if n.startswith(prefix)]


class StubDiscovery:
    """Writes the report a real Discovery would, nothing else. QFAIL raises."""
    results_dir = None
    def __init__(self, qid):
        self.university_qid, self.university_label = qid, f"University {qid}"
    def discover_missing(self):
        if self.university_qid == "QFAIL":
            raise ValueError("No LLM provider returned any units")
        d = self.results_dir / "reports"; d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.university_qid}_report.json").write_text(json.dumps(
            {"missing": 1, "exists_orphan": 0, "unresolved": 0, "extraction_provider": "openai"}))
        return []


@pytest.fixture
def stub(monkeypatch, tmp_path):
    import wikidata_discover.discovery as disc
    StubDiscovery.results_dir = tmp_path
    monkeypatch.setattr(disc, "Discovery", StubDiscovery)
    monkeypatch.setattr(batch, "source_identity", lambda run_dir: "abc123")
    return tmp_path


def test_run_records_say_which_providers_were_available_and_used(stub, monkeypatch):
    import wikidata_discover.config as config
    monkeypatch.setattr(config, "OPENAI_API_KEY", "k"); monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", None)
    bucket = FakeBucket()
    batch.run_batch("r30", ["Q1"], bucket, results_dir=stub, report=lambda m: None)
    start = json.loads(bucket.store["runs/r30/invocations.jsonl"].splitlines()[0])
    assert start["providers"] == {"openai": True, "anthropic": False, "gemini": False}
    assert json.loads(bucket.store["runs/r30/run.json"])["providers"] == start["providers"]
    rec = json.loads(bucket.store["runs/r30/log.jsonl"].splitlines()[0])
    assert rec["provider"] == "openai"                      # the one whose answer was used


def test_best_available_remembers_the_provider_it_used(monkeypatch):
    import wikidata_discover.llm_helpers as lh
    from wikidata_discover.llm_helpers import LLMHelper
    monkeypatch.setattr(LLMHelper, "extract_divisions_openai", staticmethod(lambda u, w: []))
    monkeypatch.setattr(LLMHelper, "extract_divisions_anthropic", staticmethod(lambda u, w: [{"name": "Law"}]))
    assert LLMHelper.extract_divisions_best_available("U", "https://u.edu") == [{"name": "Law"}]
    assert lh.last_extraction_provider == "anthropic"
    monkeypatch.setattr(LLMHelper, "extract_divisions_anthropic", staticmethod(lambda u, w: []))
    monkeypatch.setattr(LLMHelper, "extract_divisions_gemini", staticmethod(lambda u, w: []))
    with pytest.raises(ValueError):
        LLMHelper.extract_divisions_best_available("U", "https://u.edu")
    assert lh.last_extraction_provider is None               # a failure never keeps the old name


def test_stale_export_deletion_is_bounded_like_an_upload(stub, monkeypatch):
    import wikidata_discover.config as config
    bucket = FakeBucket({"runs/r31/missing_divisions_Q1.csv": "old", "runs/r31/quickstatements_Q1.qs": "old"})
    batch.run_batch("r31", ["Q1"], bucket, results_dir=stub, report=lambda m: None, hard_deadline_s=1800)
    ops = {(op, name.rsplit("/", 1)[-1]) for op, name, t in bucket.store["__calls__"]}
    assert ("exists", "missing_divisions_Q1.csv") in ops and ("delete", "quickstatements_Q1.qs") in ops
    assert all(5 <= t <= 60 for op, name, t in bucket.store["__calls__"])
    assert "runs/r31/missing_divisions_Q1.csv" not in bucket.store
    # Kill passed before the stale check: the call is refused, the attempt still gets recorded.
    class Late(StubDiscovery):
        def discover_missing(self):
            r = super().discover_missing(); config.HARD_DEADLINE = time.time() - 1; return r
    import wikidata_discover.discovery as disc
    monkeypatch.setattr(disc, "Discovery", Late)
    bucket = FakeBucket({"runs/r32/missing_divisions_Q1.csv": "old"})
    s = batch.run_batch("r32", ["Q1"], bucket, results_dir=stub, report=lambda m: None, hard_deadline_s=1800)
    rec = json.loads((stub / "runs" / "r32" / "log.jsonl").read_text().splitlines()[0])
    assert rec["uploaded"] is False and s["outcome"] == "failed"


def test_no_university_starts_once_the_work_deadline_is_closer_than_the_reserve(stub):
    bucket = FakeBucket()
    # 100 s to the kill means 10 s of work deadline; a 30 s reserve cannot fit.
    s = batch.run_batch("r33", ["Q1", "Q2"], bucket, results_dir=stub, report=lambda m: None,
                        hard_deadline_s=100, reserve_s=30, time_budget_s=10_000)
    assert s["stopped_for_time"] is True and s["processed"] == 0 and s["failed"] == 0
    assert not (stub / "runs" / "r33" / "log.jsonl").exists()       # no failed attempts were minted
    assert s["outcome"] == "stopped_for_time"


def test_source_identity_names_a_clean_commit_or_a_saved_patch(tmp_path):
    import subprocess
    repo = tmp_path / "repo"; (repo / "wikidata_discover").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (repo / "wikidata_discover" / "x.py").write_text("a = 1\n")
    run("add", "."); run("commit", "-q", "-m", "one")
    sha = run("rev-parse", "--short", "HEAD").stdout.strip()
    run_dir = tmp_path / "run"; run_dir.mkdir()
    assert batch.source_identity(run_dir, repo=repo) == sha and not list(run_dir.iterdir())
    (repo / "wikidata_discover" / "x.py").write_text("a = 2\n")             # tracked edit
    (repo / "wikidata_discover" / "y.py").write_text("new = True\n")        # untracked file
    identity = batch.source_identity(run_dir, repo=repo)
    assert identity.startswith(f"{sha}-dirty-") and len(identity) == len(sha) + len("-dirty-") + 12
    patch = run_dir / f"source-{identity}.patch"
    assert patch.exists()
    run("checkout", "--", "."); (repo / "wikidata_discover" / "y.py").unlink()
    run("apply", str(patch))                                                  # rebuilds the exact source
    assert (repo / "wikidata_discover" / "x.py").read_text() == "a = 2\n"
    assert (repo / "wikidata_discover" / "y.py").read_text() == "new = True\n"
    assert batch.source_identity(run_dir, repo=repo) == identity              # same tree, same name


def test_run_batch_uploads_log_under_run_prefix_and_dedupes(stub):
    bucket = FakeBucket()
    s = batch.run_batch("r1", ["Q1", "Q2", "Q1"], bucket, results_dir=stub, report=lambda m: None,
                        invocation_args={"request": {"qids": ["Q1", "Q2", "Q1"]}})
    assert (s["requested"], s["processed"], s["ok"], s["failed"]) == (2, 2, 2, 0)
    assert "runs/r1/log.jsonl" in bucket.store and "runs/r1/run.json" in bucket.store
    assert not any(k.startswith("runs/r1/runs/") for k in bucket.store)
    assert "runs/r1/reports/Q1_report.json" in bucket.store
    assert parse_done(bucket.store["runs/r1/log.jsonl"]) == {"Q1", "Q2"}
    run = json.loads(bucket.store["runs/r1/run.json"])
    assert run["args"] == {"request": {"qids": ["Q1", "Q2", "Q1"]}} and run["qids"] == ["Q1", "Q2"]


def test_run_batch_resumes_from_bucket_and_keeps_invocation_history(stub):
    prior_log = json.dumps({"qid": "Q1", "status": "ok", "uploaded": True}) + "\n"
    prior_inv = json.dumps({"started": "earlier", "qids": ["Q1"]}) + "\n"
    bucket = FakeBucket({"runs/r2/log.jsonl": prior_log, "runs/r2/invocations.jsonl": prior_inv,
                         "runs/r2/run.json": "{}"})
    t0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    s = batch.run_batch("r2", ["Q1", "Q2"], bucket, results_dir=stub, report=lambda m: None)
    assert (s["skipped_done"], s["processed"]) == (1, 1)
    assert bucket.store["runs/r2/run.json"] == "{}"                       # never rewritten on resume
    lines = [json.loads(l) for l in bucket.store["runs/r2/invocations.jsonl"].splitlines()]
    assert len(lines) == 3 and lines[0]["started"] == "earlier"
    assert lines[1]["resumed"] is True
    assert t0 <= lines[1]["started"] <= lines[2]["ended"]                 # the start is the real start
    assert lines[2]["outcome"] == "ok" and lines[2]["ended"] and lines[2]["summary"]["processed"] == 1
    assert s["outcome"] == "ok"


def test_run_batch_outcome_reflects_failures_and_time(stub):
    bucket = FakeBucket(fail_on=("log.jsonl",))
    assert batch.run_batch("r7", ["Q1"], bucket, results_dir=stub, report=lambda m: None)["outcome"] == "failed"
    # only the final metadata upload fails: the local history ends with a failed record
    bucket = FakeBucket(fail_on=("invocations.jsonl",))
    s = batch.run_batch("r7b", ["Q1"], bucket, results_dir=stub, report=lambda m: None)
    last = json.loads((stub / "runs" / "r7b" / "invocations.jsonl").read_text().splitlines()[-1])
    assert s["outcome"] == "failed" and last["outcome"] == "failed" and "note" in last
    s = batch.run_batch("r8", ["Q1"], FakeBucket(), results_dir=stub, report=lambda m: None,
                        time_budget_s=1, reserve_s=10)
    assert s["outcome"] == "stopped_for_time"
    end = json.loads((stub / "runs" / "r8" / "invocations.jsonl").read_text().splitlines()[-1])
    assert end["outcome"] == "stopped_for_time" and end["summary"]["processed"] == 0


def test_run_batch_records_invocation_even_when_nothing_runs(stub):
    prior_log = json.dumps({"qid": "Q1", "status": "ok", "uploaded": True}) + "\n"
    bucket = FakeBucket({"runs/r4/log.jsonl": prior_log, "runs/r4/run.json": "{}"})
    s = batch.run_batch("r4", ["Q1"], bucket, results_dir=stub, report=lambda m: None)
    assert s["processed"] == 0
    assert "runs/r4/invocations.jsonl" in bucket.store


def test_run_batch_reserve_stops_before_starting(stub):
    bucket = FakeBucket()
    s = batch.run_batch("r5", ["Q1"], bucket, results_dir=stub, report=lambda m: None,
                        time_budget_s=100, reserve_s=100)
    assert s["stopped_for_time"] is True and s["processed"] == 0
    s = batch.run_batch("r5", ["Q1"], bucket, results_dir=stub, report=lambda m: None,
                        time_budget_s=100, reserve_s=50)
    assert s["processed"] == 1
    assert json.loads(bucket.store["runs/r5/run.json"])["reserve_s"] == 100


def test_run_batch_restores_caches_for_pending_qids(stub):
    # Q1 ran before, used cache file c1.json, but its upload failed, so it is pending.
    # Q2 is done; its cache (c2.json) is not needed.
    log = "\n".join(json.dumps(r) for r in [
        {"qid": "Q1", "status": "ok", "uploaded": False, "cache_files": ["c1.json"]},
        {"qid": "Q2", "status": "ok", "uploaded": True, "cache_files": ["c2.json"]}]) + "\n"
    bucket = FakeBucket({"runs/r6/log.jsonl": log, "runs/r6/run.json": "{}",
                         "runs/r6/cache/c1.json": '{"cached": 1}', "runs/r6/cache/c2.json": '{"cached": 2}'})
    batch.run_batch("r6", ["Q1", "Q2"], bucket, results_dir=stub, report=lambda m: None)
    assert (stub / "cache" / "c1.json").read_text() == '{"cached": 1}'
    assert not (stub / "cache" / "c2.json").exists()


def test_restore_caches_includes_files_no_log_record_mentions(stub):
    # c3.json was uploaded by an attempt whose log record never reached the bucket.
    log = json.dumps({"qid": "Q2", "status": "ok", "uploaded": True, "cache_files": ["c2.json"]}) + "\n"
    bucket = FakeBucket({"runs/r12/log.jsonl": log,
                         "runs/r12/cache/c2.json": "done", "runs/r12/cache/c3.json": "orphaned"})
    (stub / "runs" / "r12").mkdir(parents=True)
    (stub / "runs" / "r12" / "log.jsonl").write_text(log)
    n = batch.restore_caches(bucket, "r12", stub / "runs" / "r12" / "log.jsonl", stub, done={"Q2"})
    assert n == 1 and (stub / "cache" / "c3.json").read_text() == "orphaned"
    assert not (stub / "cache" / "c2.json").exists()


def test_run_batch_removes_stale_exports_on_retry(stub):
    # An earlier attempt at Q1 uploaded a CSV and a QuickStatements file; this attempt
    # (the stub writes neither) finds nothing missing, so they must go.
    log = json.dumps({"qid": "Q1", "started": "t", "status": "ok", "uploaded": False}) + "\n"
    bucket = FakeBucket({"runs/r10/log.jsonl": log, "runs/r10/run.json": "{}",
                         "runs/r10/missing_divisions_Q1.csv": "old", "runs/r10/quickstatements_Q1.qs": "old",
                         "runs/r10/missing_divisions_Q2.csv": "other university, untouched"})
    (stub / "missing_divisions_Q1.csv").write_text("left over locally too")
    batch.run_batch("r10", ["Q1"], bucket, results_dir=stub, report=lambda m: None)
    assert "runs/r10/missing_divisions_Q1.csv" not in bucket.store
    assert "runs/r10/quickstatements_Q1.qs" not in bucket.store
    assert bucket.store["runs/r10/missing_divisions_Q2.csv"] == "other university, untouched"
    assert not (stub / "missing_divisions_Q1.csv").exists()


def test_rotated_secret_is_picked_up_on_a_warm_instance(monkeypatch):
    from wikidata_discover import config, llm_helpers
    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False); monkeypatch.setattr(config, env, None)
    monkeypatch.setenv("GOOGLE_API_KEY", "deploy-time-setting")
    monkeypatch.setattr(batch, "_injected", {})

    class Payload:
        def __init__(self, d): self.data = d
    class Resp:
        def __init__(self, d): self.payload = Payload(d)
    class Rotating:
        version = 1
        def access_secret_version(self, request, **kw):
            return Resp(f"v{self.version}-{request['name'].split('/')[3]}".encode())

    sm = Rotating()
    batch.load_keys_from_secret_manager(client=sm)
    llm_helpers._openai_client = "client built with v1"
    assert config.OPENAI_API_KEY == "v1-openai-api-key"
    sm.version = 2                                     # key rotated between invocations
    batch.load_keys_from_secret_manager(client=sm)
    assert config.OPENAI_API_KEY == "v2-openai-api-key" == os.environ["OPENAI_API_KEY"]
    assert llm_helpers._openai_client is None          # cached client dropped
    assert config.GOOGLE_API_KEY == "deploy-time-setting"   # a real env setting is kept


def test_run_batch_run_log_upload_failure_requeues_qid(stub):
    bucket = FakeBucket(fail_on=("log.jsonl",))
    s = batch.run_batch("r3", ["Q1"], bucket, results_dir=stub, report=lambda m: None)
    assert s["failed"] == 1 and s["ok"] == 1
    assert load_done(stub / "runs" / "r3" / "log.jsonl") == set()         # local log agrees: not done
    assert "runs/r3/log.jsonl" not in bucket.store


def test_correction_record_keeps_failed_status(stub):
    bucket = FakeBucket(fail_on=("log.jsonl",))
    batch.run_batch("r14", ["QFAIL"], bucket, results_dir=stub, report=lambda m: None)
    last = json.loads((stub / "runs" / "r14" / "log.jsonl").read_text().splitlines()[-1])
    assert last["status"] == "failed" and last["uploaded"] is False and "No LLM provider" in last["error"]


def test_run_batch_survives_an_unreachable_bucket(stub):
    class DeadBlob(FakeBlob):
        def exists(self, **kw): raise OSError("bucket unreachable")
        def download_as_text(self, **kw): raise OSError("bucket unreachable")
        def upload_from_filename(self, path, **kw): raise OSError("bucket unreachable")
    class DeadBucket(FakeBucket):
        def blob(self, name): return DeadBlob(self.store, name)
        def list_blobs(self, prefix="", **kw): raise OSError("bucket unreachable")
    # Cold start (no local log): refuse to run, record the failure, touch nothing else.
    t0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    s = batch.run_batch("r15", ["Q1"], DeadBucket(), results_dir=stub, report=lambda m: None)
    assert s["outcome"] == "failed" and s["processed"] == 0 and s["sync_error"].startswith("OSError")
    lines = [json.loads(l) for l in (stub / "runs" / "r15" / "refused.jsonl").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["outcome"] == "failed" and lines[0]["operator"]
    assert t0 <= lines[0]["started"] <= lines[0]["ended"] and "seconds" in lines[0]   # real start, not the end
    for name in ("run.json", "log.jsonl", "invocations.jsonl"):
        assert not (stub / "runs" / "r15" / name).exists()          # nothing that could replace history

    # Partial sync: log.jsonl came down but invocations.jsonl did not. Still refuse.
    class HalfDeadBlob(FakeBlob):
        def exists(self, **kw):
            if self.name.endswith("invocations.jsonl"): raise OSError("bucket unreachable")
            return super().exists(**kw)
    class HalfDeadBucket(FakeBucket):
        def blob(self, name): return HalfDeadBlob(self.store, name)
    prior = json.dumps({"qid": "Q1", "started": "t", "status": "ok", "uploaded": True}) + "\n"
    s = batch.run_batch("r15b", ["Q1", "Q2"], HalfDeadBucket({"runs/r15b/log.jsonl": prior}),
                        results_dir=stub, report=lambda m: None)
    assert s["outcome"] == "failed" and s["processed"] == 0
    assert not (stub / "runs" / "r15b" / "invocations.jsonl").exists()

    # Warm instance (all three run files exist locally): continue from local state and
    # record the error. Anything less is a half-synced cold start, not a warm instance.
    (stub / "runs" / "r16").mkdir(parents=True)
    (stub / "runs" / "r16" / "log.jsonl").write_text("")
    s = batch.run_batch("r16", ["Q1"], DeadBucket(), results_dir=stub, report=lambda m: None)
    assert s["outcome"] == "failed" and s["processed"] == 0
    (stub / "runs" / "r16" / "invocations.jsonl").write_text("")
    s = batch.run_batch("r16", ["Q1"], DeadBucket(), results_dir=stub, report=lambda m: None)
    assert s["outcome"] == "failed" and s["processed"] == 0
    original = json.dumps({"run_id": "r16", "operator": "first"})
    (stub / "runs" / "r16" / "run.json").write_text(original)
    s = batch.run_batch("r16", ["Q1"], DeadBucket(), results_dir=stub, report=lambda m: None)
    assert s["outcome"] == "failed" and s["processed"] == 1
    lines = [json.loads(l) for l in (stub / "runs" / "r16" / "invocations.jsonl").read_text().splitlines()]
    assert lines[0]["sync_error"].startswith("OSError") and lines[0]["operator"] and lines[0]["resumed"] is True
    assert lines[-1]["outcome"] == "failed"
    assert (stub / "runs" / "r16" / "run.json").read_text() == original    # never minted again


def test_fail_reason_marks_the_invocation_failed(stub):
    bucket = FakeBucket()
    s = batch.run_batch("r18", [], bucket, results_dir=stub, report=lambda m: None, fail_reason="list unreadable")
    assert s["outcome"] == "failed" and s["failed"] == 1 and s["fail_reason"] == "list unreadable"
    end = json.loads(bucket.store["runs/r18/invocations.jsonl"].splitlines()[-1])
    assert end["outcome"] == "failed"


def test_failed_beats_stopped_for_time(stub):
    # the first metadata upload fails, then the (tiny) time budget stops the loop
    bucket = FakeBucket(fail_on=("invocations.jsonl",))
    s = batch.run_batch("r19", ["Q1"], bucket, results_dir=stub, report=lambda m: None, time_budget_s=1e-9)
    assert s["stopped_for_time"] is True and s["failed"] >= 1 and s["outcome"] == "failed"


def test_secret_manager_client_is_built_only_when_needed(monkeypatch):
    from wikidata_discover import config
    def boom():
        raise RuntimeError("no Google credentials here")
    monkeypatch.setattr(batch, "_new_secret_manager_client", boom)
    monkeypatch.setattr(batch, "_injected", {})
    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(env, f"{env}-from-dotenv")
    assert set(batch.load_keys_from_secret_manager().values()) == {"env"}      # never touched Secret Manager
    monkeypatch.delenv("GOOGLE_API_KEY"); monkeypatch.setattr(config, "GOOGLE_API_KEY", None)
    assert batch.load_keys_from_secret_manager()["GOOGLE_API_KEY"] == "missing"  # construction failure tolerated
    assert config.OPENAI_API_KEY == "OPENAI_API_KEY-from-dotenv"


def test_operator_identity_prefers_env(monkeypatch):
    monkeypatch.setenv("ACADEMIABOT_OPERATOR", "cloud-scheduler:academiabot-collect-slice")
    assert batch.operator_identity() == "cloud-scheduler:academiabot-collect-slice"
    monkeypatch.delenv("ACADEMIABOT_OPERATOR")
    assert batch.operator_identity()   # git email or OS user, never empty
