"""snapshot (Anya, week 3): the subtree crawl, against a fake Wikidata. No network."""

import json
import re
import sys

import pytest

from wikidata_discover import snapshot as snap

E = "http://www.wikidata.org/entity/"
RANK = "http://wikiba.se/ontology#"

# A tiny Wikidata. Statements: (id, subject, prop, value, rank).
#   Q2 -P749-> Q1 (school of the university), Q1 -P527-> Q3, Q4 -P749-> Q2 (department),
#   Q2 -P749-> Q9 (a second parent outside the tree, normal rank), Q3 -P361-> Q1.
STATEMENTS = [
    ("Q2-a", "Q2", "P749", "Q1", "PreferredRank"),
    ("Q1-b", "Q1", "P527", "Q3", "NormalRank"),
    ("Q4-c", "Q4", "P749", "Q2", "NormalRank"),
    ("Q2-d", "Q2", "P749", "Q9", "NormalRank"),
    ("Q3-e", "Q3", "P361", "Q1", "NormalRank"),
]
QUALIFIERS = {"Q4-c": [("P580", "2001-01-01T00:00:00Z")], "Q2-d": [("P642", E + "Q77")]}
DETAILS = {
    "Q1": {"label": [("Big University", "en")], "P31": [E + "Q3918"], "P856": ["https://big.edu"]},
    "Q2": {"label": [("School of Law", "en"), ("Facultad de Derecho", "es")],
           "alias": [("Law School", "en")], "P31": [E + "Q31855"], "P6782": ["0abc123"]},
    "Q3": {"label": [("Medical Center", "en")]},
    "Q4": {"label": [("Département de droit", "fr")]},
    "Q9": {"label": [("Other University", "en")]},
}


def st(sid, s, p, v, r):
    return {"st": {"value": snap.STATEMENT + sid}, "prop": {"value": p}, "rank": {"value": RANK + r}}


def fake_sparql(query):
    fake_sparql.queries.append(query)
    if query.startswith("SELECT ?st ?prop ?child ?rank"):
        assert "DeprecatedRank" in query and 'hint:optimizer "None"' in query
        qid = re.search(r"ps:P749 wd:(Q\d+)", query).group(1)
        out = []
        for sid, s, p, v, r in STATEMENTS:
            if p in snap.UP and v == qid:
                out.append({**st(sid, s, p, v, r), "child": {"value": E + s}})
            if p in snap.DOWN and s == qid:
                out.append({**st(sid, s, p, v, r), "child": {"value": E + v}})
        return out
    if query.startswith("SELECT ?child ?prop ?parent"):
        kids = set(re.findall(r"wd:(Q\d+)", query.split("VALUES ?child")[1].split("}")[0]))
        return [{**st(sid, s, p, v, r), "child": {"value": E + s}, "parent": {"value": E + v}}
                for sid, s, p, v, r in STATEMENTS if p in snap.UP and s in kids]
    if query.startswith("SELECT ?st ?qual ?value"):
        sts = re.findall(r"statement/([^>]+)>", query)
        return [{"st": {"value": snap.STATEMENT + s}, "qual": {"value": snap.QUALIFIER + q},
                 "value": {"value": v}} for s in sts for q, v in QUALIFIERS.get(s, [])]
    if query.startswith("SELECT ?item ?kind"):
        items = re.findall(r"wd:(Q\d+)", query.split("VALUES ?item")[1].split("}")[0])
        out = []
        for q in items:
            d = DETAILS.get(q, {})
            for kind in ("label", "alias"):
                for text, lang in d.get(kind, []):
                    out.append({"item": {"value": E + q}, "kind": {"value": kind},
                                "value": {"value": text, "xml:lang": lang}})
            for kind in snap.DETAIL_PROPS:
                for v in d.get(kind, []):
                    out.append({"item": {"value": E + q}, "kind": {"value": kind}, "value": {"value": v},
                                "rank": {"value": RANK + "NormalRank"}})
        return out
    if query.startswith("SELECT ?item ?label"):
        names = {"Q3918": "university", "Q31855": "college"}
        return [{"item": {"value": E + q}, "label": {"value": names[q]}}
                for q in re.findall(r"wd:(Q\d+)", query) if q in names]
    raise AssertionError("unexpected query: " + query[:80])


@pytest.fixture
def wd():
    fake_sparql.queries = []
    return fake_sparql


def test_snapshot_finds_every_unit_through_up_and_down_links(wd):
    s = snap.take_snapshot("Q1", sparql=wd)
    tree = {q for q, u in s["units"].items() if u["in_tree"]}
    assert tree == {"Q1", "Q2", "Q3", "Q4"}
    assert s["units"]["Q4"]["depth"] == 2 and s["counts"]["units_in_tree"] == 3


def test_a_second_parent_outside_the_tree_is_kept_with_its_rank(wd):
    s = snap.take_snapshot("Q1", sparql=wd)
    parents = {(p["parent"], p["prop"], p["rank"]) for p in s["units"]["Q2"]["parents"]}
    assert parents == {("Q1", "P749", "preferred"), ("Q9", "P749", "normal")}
    assert s["units"]["Q9"]["in_tree"] is False and s["units"]["Q9"]["label"] == "Other University"
    outside = [l for l in s["links"] if l["parent"] == "Q9"]
    assert outside[0]["parent_in_tree"] is False


def test_qualifiers_are_kept_on_each_link(wd):
    s = snap.take_snapshot("Q1", sparql=wd)
    by_id = {l["statement_id"]: l for l in s["links"]}
    assert by_id["Q4-c"]["qualifiers"] == {"P580": ["2001-01-01T00:00:00Z"]}
    assert by_id["Q2-d"]["qualifiers"] == {"P642": ["Q77"]}      # item values become QIDs
    assert by_id["Q1-b"]["statement_on"] == "Q1" and by_id["Q4-c"]["statement_on"] == "Q4"


def test_unit_details_keep_names_types_website_and_ror(wd):
    u = snap.take_snapshot("Q1", sparql=wd)["units"]
    assert u["Q2"]["labels"] == {"en": "School of Law", "es": "Facultad de Derecho"}
    assert u["Q2"]["aliases"] == {"en": ["Law School"]}
    assert u["Q2"]["P31"] == [{"qid": "Q31855", "rank": "normal", "label": "college"}]
    assert u["Q2"]["P6782"] == [{"value": "0abc123", "rank": "normal"}]
    assert u["Q1"]["P856"] == [{"value": "https://big.edu", "rank": "normal"}]
    assert u["Q4"]["label"] == "Département de droit"           # no English label: another one


def test_a_cycle_is_crawled_once(wd, monkeypatch):
    monkeypatch.setattr(fake_sparql, "queries", [])
    cyc = STATEMENTS + [("Q1-z", "Q1", "P749", "Q4", "NormalRank")]   # Q1 -P749-> Q4: a loop
    monkeypatch.setattr(sys.modules[__name__], "STATEMENTS", cyc)
    s = snap.take_snapshot("Q1", sparql=wd)
    assert len([q for q in wd.queries if q.startswith("SELECT ?st ?prop ?child")]) == 4


def test_max_units_marks_the_snapshot_truncated(wd):
    s = snap.take_snapshot("Q1", sparql=wd, max_units=2)
    assert s["truncated"] is True and s["counts"]["units_in_tree"] == 1


# ── the command: run log, bucket, resume ──────────────────────────────────

class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_string(self, text, content_type=None, timeout=None, **kw):
        self.bucket.store[self.name] = text


class FakeClient:
    def __init__(self, bucket):
        self.bucket = bucket

    def list_blobs(self, bucket, prefix=""):
        return [FakeBlob(bucket, n) for n in sorted(bucket.store) if n.startswith(prefix)]


class FakeBucket:
    name = "fake-bucket"

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.client = FakeClient(self)

    def blob(self, name):
        return FakeBlob(self, name)


def test_run_saves_each_snapshot_under_the_run_folder_and_skips_done_ones(wd, tmp_path):
    bucket = FakeBucket({"runs/snap-1/snapshots/Q5.json": "{}"})
    s = snap.run_snapshots(["Q5", "Q1"], "snap-1", ["snapshot"], bucket=bucket, sparql=wd,
                           results_dir=tmp_path, report=lambda m: None)
    assert (s["skipped_done"], s["ok"], s["failed"]) == (1, 1, 0)
    saved = json.loads(bucket.store["runs/snap-1/snapshots/Q1.json"])
    assert saved["run_id"] == "snap-1" and saved["university_qid"] == "Q1"
    assert (tmp_path / "snapshots" / "Q1.json").exists()
    assert (tmp_path / "runs" / "snap-1" / "snapshots" / "Q1.json").exists()
    progress = bucket.store["runs/snap-1/progress.jsonl"].splitlines()
    assert [json.loads(l)["qid"] for l in progress] == ["Q1"]


def test_a_failed_university_is_recorded_and_retried_next_time(tmp_path):
    def broken(query):
        raise RuntimeError("query service down")
    bucket = FakeBucket()
    s = snap.run_snapshots(["Q1"], "snap-2", [], bucket=bucket, sparql=broken,
                           results_dir=tmp_path, report=lambda m: None)
    assert s["failed_qids"] == ["Q1"] and s["outcome"] == "partial"
    assert "runs/snap-2/snapshots/Q1.json" not in bucket.store
    assert snap.done_in_bucket(bucket, "snap-2") == set()


def test_run_writes_one_runs_row(wd, tmp_path, monkeypatch):
    from wikidata_discover import run_log
    rows = []
    monkeypatch.setattr(run_log, "DISABLED", False)
    monkeypatch.setattr(run_log.RunLog, "_load_bigquery", lambda self, r: rows.append(r))
    snap.run_snapshots(["Q1"], "snap-3", ["snapshot", "Q1"], bucket=FakeBucket(), sparql=wd,
                       results_dir=tmp_path, report=lambda m: None)
    runs = [r for batch in rows for r in batch["runs"]]
    assert len(runs) == 1 and runs[0]["command"] == "snapshot" and runs[0]["outcome"] == "ok"
    assert runs[0]["qids"] == ["Q1"] and json.loads(runs[0]["summary"])["ok"] == 1


def test_a_bad_run_id_is_refused(tmp_path):
    with pytest.raises(ValueError):
        snap.run_snapshots(["Q1"], "../x", [], bucket=FakeBucket(), sparql=fake_sparql,
                           results_dir=tmp_path, report=lambda m: None)
