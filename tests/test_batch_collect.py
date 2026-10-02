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
        def access_secret_version(self, request):
            return Resp(f"secret-for-{request['name'].split('/')[3]}\n".encode())

    batch.load_keys_from_secret_manager(client=FakeSM())
    assert config.OPENAI_API_KEY == "sk-from-env"                     # env wins, config updated
    assert config.ANTHROPIC_API_KEY == "secret-for-anthropic-api-key"  # stripped
    assert os.environ["GOOGLE_API_KEY"] == "secret-for-gemini-api-key"


def test_missing_optional_secret_is_tolerated(monkeypatch):
    from wikidata_discover import config
    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False); monkeypatch.setattr(config, env, None)

    class Payload:
        def __init__(self, d): self.data = d
    class Resp:
        def __init__(self, d): self.payload = Payload(d)
    class OnlyOpenAI:
        def access_secret_version(self, request):
            if "openai" in request["name"]:
                return Resp(b"sk-only")
            raise PermissionError("denied")

    assert batch.load_keys_from_secret_manager(client=OnlyOpenAI()) == {
        "OPENAI_API_KEY": "secret_manager", "ANTHROPIC_API_KEY": "missing", "GOOGLE_API_KEY": "missing"}
    assert config.OPENAI_API_KEY == "sk-only" and config.ANTHROPIC_API_KEY is None

    class Nothing:
        def access_secret_version(self, request):
            raise PermissionError("denied")
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)   # the first call exported it
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
    def exists(self):
        return self.name in self.store
    def download_as_text(self):
        return self.store[self.name]
    def upload_from_filename(self, path):
        if any(self.name.endswith(s) for s in self.fail_on):
            raise OSError(f"simulated upload failure for {self.name}")
        self.store[self.name] = Path(path).read_text()


class FakeBucket:
    def __init__(self, store=None, fail_on=()):
        self.store = {} if store is None else store
        self.fail_on = fail_on
    def blob(self, name):
        return FakeBlob(self.store, name, self.fail_on)


class StubDiscovery:
    """Writes the report a real Discovery would, nothing else."""
    results_dir = None
    def __init__(self, qid):
        self.university_qid, self.university_label = qid, f"University {qid}"
    def discover_missing(self):
        d = self.results_dir / "reports"; d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.university_qid}_report.json").write_text(json.dumps({"missing": 1, "exists_orphan": 0, "unresolved": 0}))
        return []


@pytest.fixture
def stub(monkeypatch, tmp_path):
    import wikidata_discover.discovery as disc
    StubDiscovery.results_dir = tmp_path
    monkeypatch.setattr(disc, "Discovery", StubDiscovery)
    monkeypatch.setattr(batch, "git_commit", lambda: "abc123")
    return tmp_path


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
    s = batch.run_batch("r2", ["Q1", "Q2"], bucket, results_dir=stub, report=lambda m: None)
    assert (s["skipped_done"], s["processed"]) == (1, 1)
    assert bucket.store["runs/r2/run.json"] == "{}"                       # never rewritten on resume
    lines = bucket.store["runs/r2/invocations.jsonl"].splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["started"] == "earlier"
    assert json.loads(lines[1])["resumed"] is True


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


def test_run_batch_run_log_upload_failure_requeues_qid(stub):
    bucket = FakeBucket(fail_on=("log.jsonl",))
    s = batch.run_batch("r3", ["Q1"], bucket, results_dir=stub, report=lambda m: None)
    assert s["failed"] == 1 and s["ok"] == 1
    assert load_done(stub / "runs" / "r3" / "log.jsonl") == set()         # local log agrees: not done
    assert "runs/r3/log.jsonl" not in bucket.store
