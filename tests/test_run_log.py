"""Run log (Anya, week 2): rows for runs, llm_calls, evidence, candidates; texts in the
bucket; local JSON when BigQuery or the bucket fails. Fake clients only, no network."""

import json

import pytest

import wikidata_discover.config as config
import wikidata_discover.discovery as disc
import wikidata_discover.llm_helpers as lh
from wikidata_discover import run_log
from wikidata_discover.llm_helpers import LLMHelper


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_string(self, text, content_type=None, timeout=None, **kw):
        if self.bucket.broken:
            raise OSError("404 bucket does not exist")
        self.bucket.store[self.name] = text


class FakeBucket:
    name = "fake-bucket"

    def __init__(self, broken=False):
        self.store, self.broken = {}, broken

    def blob(self, name):
        return FakeBlob(self, name)


class FakeJob:
    def result(self, timeout=None):
        return None


class FakeBigQuery:
    def __init__(self, broken=False):
        self.tables, self.broken, self.calls = {}, broken, 0

    def load_table_from_json(self, rows, table, job_config=None, timeout=None):
        self.calls += 1
        if self.broken:
            raise OSError("BigQuery unreachable")
        self.tables.setdefault(table.split(".")[-1], []).extend(rows)
        return FakeJob()


@pytest.fixture
def make_log(monkeypatch, tmp_path):
    """A current RunLog with fake clients. Returns (log, bucket, bigquery)."""
    monkeypatch.setattr(run_log, "DISABLED", False)
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path / "cache")

    def make(bucket_broken=False, bq_broken=False):
        bucket, bq = FakeBucket(bucket_broken), FakeBigQuery(bq_broken)
        log = run_log.start("r1", "inv1", command="discover", args={"argv": ["discover", "Q1"]},
                            qids=["Q1"], who="anya", git_commit="abc", run_config={"depth": 1},
                            bucket=bucket, bq_client=bq, results_dir=tmp_path)
        return log, bucket, bq
    return make


# ── pure helpers ─────────────────────────────────────────────────────────

def test_clean_row_keeps_schema_columns_and_encodes_json():
    row = run_log.clean_row("llm_calls", {"llm_call_id": "a", "run_id": "r", "invocation_id": "i",
                                          "request_config": {"tools": ["web_search_preview"]},
                                          "not_a_column": 1})
    assert "not_a_column" not in row
    assert json.loads(row["request_config"]) == {"tools": ["web_search_preview"]}
    assert set(row) == {n for n, _, _ in run_log.SCHEMAS["llm_calls"]}
    assert run_log.clean_row("candidates", {})["llm_call_ids"] == []          # REPEATED is never null


def test_every_table_has_its_id_and_the_run_ids():
    for table, id_col in (("runs", "invocation_id"), ("llm_calls", "llm_call_id"),
                          ("evidence", "evidence_id"), ("candidates", "candidate_id")):
        cols = {n for n, _, _ in run_log.SCHEMAS[table]}
        assert {id_col, "run_id", "invocation_id"} <= cols


def test_new_run_id_is_a_safe_path_component():
    from wikidata_discover.batch import validate_run_id
    rid = run_log.new_run_id("discover")
    assert rid.startswith("discover-") and validate_run_id(rid) == rid


def test_no_log_is_active_unless_started():
    assert run_log.current() is None
    assert lh._record(provider="openai", model="m", purpose="extract", prompt="p") is None


# ── storage and fallback ────────────────────────────────────────────────

def test_texts_go_to_the_bucket_and_rows_to_bigquery(make_log):
    log, bucket, bq = make_log()
    cid = log.record_llm_call(provider="openai", model="gpt-4o", purpose="extract", prompt={"x": 1},
                              raw_response='{"units": [{"name": "Stern School of Business"}]}')
    log.add_candidate(name="Stern", status="missing", llm_call_ids=[cid])
    log.flush()
    call = bq.tables["llm_calls"][0]
    assert call["prompt_path"] == f"gs://fake-bucket/runs/r1/llm_calls/{cid}/prompt.json"
    assert call["response_path"] == f"gs://fake-bucket/runs/r1/llm_calls/{cid}/response.txt"
    assert "Stern School of Business" in bucket.store[f"runs/r1/llm_calls/{cid}/response.txt"]
    assert call["raw_response_available"] is True and call["invocation_id"] == "inv1"
    assert bq.tables["candidates"][0]["llm_call_ids"] == [cid]
    status = log.finish("ok")
    run = bq.tables["runs"][0]
    assert (run["run_id"], run["invocation_id"], run["who"], run["outcome"]) == ("r1", "inv1", "anya", "ok")
    assert json.loads(run["args"]) == {"argv": ["discover", "Q1"]} and run["ended_at"]
    assert status["local_dir"] is None and run_log.current() is None


def test_a_missing_bucket_keeps_texts_and_all_four_tables_locally(make_log, tmp_path):
    log, bucket, bq = make_log(bucket_broken=True)
    cid = log.record_llm_call(provider="openai", model="m", purpose="extract", prompt="p", raw_response="Law")
    log.add_candidate(name="Law", status="missing", llm_call_ids=[cid])
    log.flush()
    status = log.finish("ok")
    local = tmp_path / "runs" / "r1" / "runlog" / "inv1"
    assert status["local_dir"] == str(local) and status["bucket"].startswith("failed")
    assert (local / "text" / "llm_calls" / cid / "response.txt").read_text() == "Law"
    for t in run_log.TABLES:
        assert (local / f"{t}.jsonl").exists()                         # evidence.jsonl too, empty
    assert (local / "evidence.jsonl").read_text() == ""
    call = json.loads((local / "llm_calls.jsonl").read_text())
    assert call["response_path"] == str(local / "text" / "llm_calls" / cid / "response.txt")
    run = json.loads((local / "runs.jsonl").read_text())
    assert "bucket" in json.loads(run["storage_problems"])


def test_a_bigquery_failure_writes_local_json_and_is_not_retried(make_log, tmp_path):
    log, bucket, bq = make_log(bq_broken=True)
    log.add_candidate(name="A", status="linked")
    log.flush()
    log.add_candidate(name="B", status="orphan")
    log.flush()
    log.finish("ok")
    assert bq.calls == 1                                                # one timeout, not one per flush
    local = tmp_path / "runs" / "r1" / "runlog" / "inv1"
    names = [json.loads(line)["name"] for line in (local / "candidates.jsonl").read_text().splitlines()]
    assert names == ["A", "B"]
    assert json.loads((local / "runs.jsonl").read_text())["outcome"] == "ok"


def test_candidate_status_must_be_one_of_four(make_log):
    log, _, _ = make_log()
    with pytest.raises(ValueError):
        log.add_candidate(name="X", status="exists_linked")


def test_a_cache_hit_without_a_saved_call_says_there_is_no_raw_response(make_log):
    log, _, bq = make_log()
    log.record_llm_call(provider="openai", model="m", purpose="extract", prompt="p", cache_hit=True)
    log.flush()
    row = bq.tables["llm_calls"][0]
    assert row["cache_hit"] is True and row["raw_response_available"] is False
    assert row["response_path"] is None and row["note"] == run_log.NO_RAW_RESPONSE_NOTE


# ── extraction and match calls ──────────────────────────────────────────

def _openai_client(texts, calls):
    class R:
        def __init__(self, text):
            self.output_text, self.output = text, [1]

    class Client:
        def with_options(self, **kw):
            return self

        class responses:
            @staticmethod
            def create(**kw):
                calls.append(kw)
                return R(texts.pop(0))
    return Client()


def test_extraction_saves_each_raw_response_before_parsing_and_links_the_cache(make_log, monkeypatch):
    log, bucket, bq = make_log()
    calls = []
    good = '{"units": [{"name": "Tandon School of Engineering", "unit_type": "school", "city": "", ' \
           '"state": "", "website": null}], "reference": "https://nyu.edu/schools"}'
    monkeypatch.setattr(lh, "_get_openai_client", lambda: _openai_client(["not json", good], calls))
    units = LLMHelper.extract_divisions_openai("NYU", "https://nyu.edu")
    assert units[0]["reference"] == "https://nyu.edu/schools"
    first_id = lh.last_extraction_call_id
    again = LLMHelper.extract_divisions_openai("NYU", "https://nyu.edu")   # from the cache now
    assert again == units and len(calls) == 2
    log.flush()
    rows = bq.tables["llm_calls"]
    assert [r["purpose"] for r in rows] == ["extract"] * 3
    assert [r["cache_hit"] for r in rows] == [False, False, True]
    assert bucket.store[rows[0]["response_path"].split("fake-bucket/")[1]] == "not json"   # unparsable, still kept
    assert rows[1]["llm_call_id"] == first_id
    assert rows[2]["cached_from_llm_call_id"] == first_id                # the hit points to the real call
    assert rows[2]["response_path"] == rows[1]["response_path"] and rows[2]["raw_response_available"]
    assert json.loads(rows[0]["request_config"])["tools"] == ["web_search_preview"]
    prompt = json.loads(bucket.store[rows[0]["prompt_path"].split("fake-bucket/")[1]])
    assert prompt["input"][1]["content"] == "NYU -- https://nyu.edu"


def test_match_calls_are_recorded_with_every_choice_shown(make_log, monkeypatch):
    log, bucket, bq = make_log()
    monkeypatch.setattr(config, "OPENAI_API_KEY", "k")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", None)
    calls = []
    monkeypatch.setattr(lh, "_get_openai_client", lambda: _openai_client(["ORPHAN:Q2"], calls))
    children = [("Q1", "Stern School of Business"), ("Q2", "School of Law"), ("Q3", "Law Library")]
    assert LLMHelper.choose_match("Law School", "NYU", children) == ("ORPHAN:Q2", "School of Law")
    first = list(lh.last_match_call_ids)
    LLMHelper.choose_match("Law School", "NYU", children)               # cached decision
    log.flush()
    rows = bq.tables["llm_calls"]
    assert [r["purpose"] for r in rows] == ["match", "match"] and [r["cache_hit"] for r in rows] == [False, True]
    assert first == [rows[0]["llm_call_id"]] and lh.last_match_call_ids == [rows[1]["llm_call_id"]]
    prompt = json.loads(bucket.store[rows[0]["prompt_path"].split("fake-bucket/")[1]])
    assert [c["qid"] for c in prompt["choices"]] == ["Q1", "Q2", "Q3"]
    assert "Q3 -- Law Library" in prompt["text"]
    assert bucket.store[rows[0]["response_path"].split("fake-bucket/")[1]] == "ORPHAN:Q2"
    assert rows[1]["cached_from_llm_call_id"] == rows[0]["llm_call_id"]


def test_a_failed_match_call_is_recorded_with_its_error(make_log, monkeypatch):
    log, _, bq = make_log()
    monkeypatch.setattr(config, "OPENAI_API_KEY", "k")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", None)

    class Client:
        def with_options(self, **kw):
            return self

        class responses:
            @staticmethod
            def create(**kw):
                raise TimeoutError("slow")
    monkeypatch.setattr(lh, "_get_openai_client", lambda: Client())
    monkeypatch.setattr(lh, "_get_anthropic_client", lambda: (_ for _ in ()).throw(ValueError("no key")))
    monkeypatch.setattr(lh, "_get_gemini_client", lambda: (_ for _ in ()).throw(ValueError("no key")))
    with pytest.raises(lh.LLMUnavailable):
        LLMHelper.choose_match("Law School", "NYU", [("Q2", "School of Law")])
    log.flush()
    rows = bq.tables["llm_calls"]
    assert len(rows) == 1 and rows[0]["error"].startswith("TimeoutError")   # no row for unconfigured ones
    assert rows[0]["raw_response_available"] is False


# ── candidates ──────────────────────────────────────────────────────────

def test_candidate_row_maps_every_status():
    div = {"name": "Law", "unit_type": "school", "website": "https://law.x.edu", "reference": "https://x.edu/s"}
    linked = disc.candidate_row("Q1", div, "Law", "exists_linked -> Q5 (Law)", ("Q5", "Law"), "fuzzy", [],
                                {"Q5"}, ["c1"], "openai")
    assert (linked["status"], linked["matched_qid"], linked["source_url"]) == ("linked", "Q5", "https://x.edu/s")
    orphan = disc.candidate_row("Q1", div, "Law", "exists_orphan -> Q7", ("ORPHAN:Q7", "Law School"), "llm",
                                [("Q5", "Art"), ("Q7", "Law School")], {"Q5"}, ["c1", "m1"], "openai")
    assert (orphan["status"], orphan["matched_qid"]) == ("orphan", "Q7")
    assert orphan["match_choices"] == [{"qid": "Q5", "label": "Art", "source": "linked_child"},
                                       {"qid": "Q7", "label": "Law School", "source": "search"}]
    assert disc.candidate_row("Q1", div, "Law", "missing", None, "llm", [], set(), [], None)["status"] == "missing"
    assert disc.candidate_row("Q1", div, "Law", "unresolved", None, "llm", [], set(), [], None)["matched_qid"] is None


def test_discovery_writes_a_candidate_row_for_every_unit(make_log, monkeypatch, tmp_path):
    log, _, bq = make_log()
    monkeypatch.setattr(disc, "RESULTS_DIR", tmp_path)
    d = disc.Discovery.__new__(disc.Discovery)
    d.university_qid, d.university_label, d.university_website, d.university_lang = "Q1", "Test U", None, "en"
    monkeypatch.setattr(d, "get_existing_children", lambda: [("Q5", "School of Art")])
    monkeypatch.setattr(d, "get_all_descendants_qids", lambda: {"Q7"})
    monkeypatch.setattr(d, "get_children_alt_labels", lambda: {})
    monkeypatch.setattr(d, "search_wikidata", lambda name: [("Q7", "Law School")])

    def extract(u, w):
        lh.last_extraction_call_id = "extract-1"
        return [{"name": "School of Art"}, {"name": "School of Law"}, {"name": "New School"}, {"name": "Lost"}]

    def match(candidate, univ, children):
        lh.last_match_call_ids = [f"match-{candidate}"]
        if candidate == "School of Law":
            return ("Q7", "Law School")
        if candidate == "Lost":
            raise lh.LLMUnavailable("no provider")
        return None
    monkeypatch.setattr(disc.LLMHelper, "extract_divisions_best_available", staticmethod(extract))
    monkeypatch.setattr(disc.LLMHelper, "choose_match", staticmethod(match))
    d.discover_missing()
    log.flush()
    rows = {r["name"]: r for r in bq.tables["candidates"]}
    assert {n: r["status"] for n, r in rows.items()} == {
        "School of Art": "linked", "School of Law": "orphan", "New School": "missing", "Lost": "unresolved"}
    assert rows["School of Art"]["llm_call_ids"] == ["extract-1"] and rows["School of Art"]["match_method"] == "fuzzy"
    assert rows["School of Law"]["llm_call_ids"] == ["extract-1", "match-School of Law"]
    assert all(r["university_qid"] == "Q1" == r["parent_qid"] for r in rows.values())


# ── API keys ────────────────────────────────────────────────────────────

def test_keys_set_locally_never_contact_secret_manager(monkeypatch):
    for name in config.API_KEY_NAMES:
        monkeypatch.setenv(name, "k")
    monkeypatch.setattr(config, "_new_secret_client", lambda: pytest.fail("Secret Manager contacted"))
    assert set(config.load_api_keys().values()) <= {"env", "dotenv"}


def test_a_key_missing_locally_comes_from_secret_manager(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(config, "_ENV_BEFORE_DOTENV", {"OPENAI_API_KEY"})
    # load_missing_keys() (from master) does the loading; load_api_keys() keeps "env".
    monkeypatch.setattr(config, "load_missing_keys", lambda: {
        "OPENAI_API_KEY": "env", "ANTHROPIC_API_KEY": "secret_manager", "GOOGLE_API_KEY": "missing"})
    assert config.load_api_keys() == {"OPENAI_API_KEY": "env", "ANTHROPIC_API_KEY": "secret_manager",
                                      "GOOGLE_API_KEY": "missing"}


def test_key_sources_tell_environment_from_dotenv(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "a")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "b")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(config, "_ENV_BEFORE_DOTENV", {"OPENAI_API_KEY"})
    assert config.key_sources_before_secret_manager() == {
        "OPENAI_API_KEY": "env", "ANTHROPIC_API_KEY": "dotenv", "GOOGLE_API_KEY": "missing"}


# ── batch runner ────────────────────────────────────────────────────────

def test_a_batch_invocation_writes_one_runs_row_under_its_ids(monkeypatch, tmp_path):
    import wikidata_discover.batch as batch
    from tests.test_batch_collect import FakeBucket as BatchBucket, StubDiscovery
    monkeypatch.setattr(run_log, "DISABLED", False)
    bq = FakeBigQuery()
    real_start = run_log.start
    monkeypatch.setattr(run_log, "start", lambda *a, **k: real_start(*a, **{**k, "bq_client": bq}))
    StubDiscovery.results_dir = tmp_path
    monkeypatch.setattr(disc, "Discovery", StubDiscovery)
    monkeypatch.setattr(batch, "source_identity", lambda run_dir: "abc123")
    s = batch.run_batch("r9", ["Q1"], BatchBucket(), results_dir=tmp_path, report=lambda m: None)
    runs = bq.tables["runs"]
    assert len(runs) == 1 and runs[0]["run_id"] == "r9" and runs[0]["command"] == "batch"
    start = json.loads((tmp_path / "runs" / "r9" / "invocations.jsonl").read_text().splitlines()[0])
    assert runs[0]["invocation_id"] == start["invocation_id"] and runs[0]["git_commit"] == "abc123"
    assert s["run_log"]["bigquery"] == "ok"


def test_a_separate_cache_folder_can_be_set_without_touching_results_cache(tmp_path):
    import subprocess, sys
    out = subprocess.run([sys.executable, "-c", "from wikidata_discover import llm_helpers; print(llm_helpers._CACHE_DIR)"],
                         capture_output=True, text=True, check=True,
                         env={**__import__("os").environ, "ACADEMIABOT_CACHE_DIR": str(tmp_path / "fresh")})
    assert out.stdout.strip() == str(tmp_path / "fresh")


def test_schema_doc_lists_every_column():
    """docs/RUN_LOG_SCHEMA.md names every BigQuery column, so the page for readers
    cannot fall behind the schema in run_log.py."""
    import re
    from pathlib import Path
    doc = (Path(__file__).parent.parent / "docs" / "RUN_LOG_SCHEMA.md").read_text()
    for table, columns in run_log.SCHEMAS.items():
        section = doc.split(f"## {table}\n", 1)[1].split("\n## ", 1)[0]
        named = set(re.findall(r"^\| ([a-z_, ]+) \|", section, flags=re.M))
        named = {c.strip() for cell in named for c in cell.split(",")}
        missing = [c for c, _, _ in columns if c not in named]
        assert not missing, f"{table}: {missing} not in docs/RUN_LOG_SCHEMA.md"


def test_new_rows_are_not_legacy(make_log):
    log, bucket, bq = make_log()
    log.record_llm_call(provider="openai", model="m", purpose="extract", prompt="p", raw_response="r")
    log.add_candidate(name="School of Law", status="missing")
    assert log.pending["llm_calls"][0]["legacy"] is False
    assert log.pending["candidates"][0]["legacy"] is False


def test_setup_finds_the_columns_an_old_table_lacks():
    old = [c[0] for c in run_log.SCHEMAS["candidates"] if c[0] not in ("legacy", "source_file")]
    assert [c[0] for c in run_log.missing_columns("candidates", old)] == ["legacy", "source_file"]
    assert run_log.missing_columns("runs", [c[0] for c in run_log.SCHEMAS["runs"]]) == []
