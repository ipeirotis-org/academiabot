"""Import of run us-tier1 as legacy rows (Anya, week 3). Pure functions only, no network."""

from wikidata_discover import legacy_import as li
from wikidata_discover import run_log

EXTRACT = [
    {"name": "School of Law", "unit_type": "School", "website": "https://law.x.edu"},
    {"name": "College of Arts", "unit_type": "College", "website": "https://arts.x.edu"},
    {"name": "Career Services", "unit_type": "Office", "website": ""},
    {"name": "School of Nursing", "unit_type": "School", "website": "https://nurse.x.edu"},
]
CSV = ("name,unit_type,url,location,university_qid,university_label,status,qid\n"
       "Career Services,Office,,,Q1,X University,missing,\n"
       "School of Nursing,,,,Q1,X University,orphan,Q77\n")
REPORT = {"total_candidates": 4, "exists_linked": 2, "exists_orphan": 1, "missing": 1, "unresolved": 0,
          "unresolved_candidates": []}
MODELS = {"openai": "gpt-4o", "anthropic": "claude-opus-4-6"}


def rec(**kw):
    r = {"qid": "Q1", "status": "ok", "provider": "openai", "invocation_id": "inv9",
         "started": "2026-10-03T05:07:07Z", "report": REPORT,
         "cache_files": ["ext.json", "m1.json"]}
    r.update(kw)
    return r


FILES = {"ext.json": EXTRACT, "m1.json": {"answer": "ORPHAN:Q77", "provider": "openai"}}


def test_every_unit_gets_a_row_and_linked_units_come_from_the_extraction_answer():
    calls, cands, problem = li.build_university(rec(), FILES, CSV, MODELS)
    assert problem is None
    by = {c["name"]: c for c in cands}
    assert {n: c["status"] for n, c in by.items()} == {
        "School of Law": "linked", "College of Arts": "linked",
        "Career Services": "missing", "School of Nursing": "orphan"}
    assert by["School of Nursing"]["matched_qid"] == "Q77"
    # us-tier1 never saved what a linked unit matched: left empty, never guessed
    assert by["School of Law"]["matched_qid"] is None and by["School of Law"]["matched_label"] is None
    assert by["School of Law"]["match_method"] == "legacy_unknown" and by["Career Services"]["match_method"] == "none"


def test_legacy_rows_say_where_they_came_from_and_have_no_raw_response():
    calls, cands, _ = li.build_university(rec(), FILES, CSV, MODELS)
    assert all(c["legacy"] is True and c["run_id"] == "us-tier1" and c["evidence_ids"] == [] for c in cands)
    by = {c["name"]: c for c in cands}
    assert by["Career Services"]["source_file"] == "gs://academiabot/runs/us-tier1/missing_divisions_Q1.csv"
    assert by["School of Law"]["source_file"] == "gs://academiabot/runs/us-tier1/cache/ext.json"
    assert by["School of Law"]["source_url"] is None and by["School of Law"]["website"] == "https://law.x.edu"
    ext = [c for c in calls if c["purpose"] == "extract"][0]
    assert ext["raw_response_available"] is False and ext["legacy"] is True
    assert ext.get("prompt_path") is None and ext.get("response_path") is None
    assert ext["model"] == "gpt-4o" and "no prompt and no raw response" in ext["note"]
    assert all(c["llm_call_ids"] == [ext["llm_call_id"]] for c in cands)
    assert {c["purpose"] for c in calls} == {"extract", "match"}


def test_ids_are_stable_so_a_second_build_gives_the_same_rows():
    a = li.build_university(rec(), FILES, CSV, MODELS)
    b = li.build_university(rec(), FILES, CSV, MODELS)
    assert a == b


def test_counts_that_differ_from_the_report_stop_the_university():
    bad = dict(REPORT, exists_linked=3, missing=0)
    calls, cands, problem = li.build_university(rec(report=bad), FILES, CSV, MODELS)
    assert cands == [] and "differ from the report" in problem
    assert calls   # the calls are still recorded


def test_an_empty_answer_next_to_a_real_one_is_kept_as_a_call_only():
    files = dict(FILES, **{"empty.json": []})
    r = rec(cache_files=["ext.json", "m1.json", "empty.json"])
    calls, cands, problem = li.build_university(r, files, CSV, MODELS)
    assert problem is None and len(cands) == 4
    empty = [c for c in calls if c["source_file"].endswith("empty.json")][0]
    assert empty["provider"] is None and "empty extraction answer" in empty["note"]


def test_two_non_empty_answers_or_a_failed_attempt_are_listed_for_a_person():
    files = dict(FILES, **{"ext2.json": EXTRACT[:1]})
    _, cands, problem = li.build_university(rec(cache_files=["ext.json", "ext2.json"]), files, CSV, MODELS)
    assert cands == [] and "2 non-empty" in problem
    _, cands, problem = li.build_university(rec(status="failed", error="No units"), {}, None, MODELS)
    assert cands == [] and "attempt failed" in problem


def test_a_csv_row_not_in_the_answer_stops_the_university():
    csv = CSV + "Ghost School,,,,Q1,X University,missing,\n"
    _, cands, problem = li.build_university(rec(), FILES, csv, MODELS)
    assert cands == [] and "Ghost School" in problem


def test_unresolved_units_come_from_the_report():
    report = dict(REPORT, exists_linked=1, unresolved=1,
                  unresolved_candidates=[{"name": "College of Arts", "url": ""}])
    _, cands, problem = li.build_university(rec(report=report), FILES, CSV, MODELS)
    assert problem is None
    assert {c["name"]: c["status"] for c in cands}["College of Arts"] == "unresolved"


def test_matched_labels_are_added_for_every_matched_qid():
    _, cands, _ = li.build_university(rec(), FILES, CSV, MODELS)
    n = li.add_matched_labels(cands, lambda qids: {q: "Label " + q for q in qids})
    assert n == 1 and [c["matched_label"] for c in cands if c["matched_qid"]] == ["Label Q77"]


def test_rows_fit_the_bigquery_schema():
    calls, cands, _ = li.build_university(rec(), FILES, CSV, MODELS)
    for c in cands:
        assert set(run_log.clean_row("candidates", c)) == {n for n, _, _ in run_log.SCHEMAS["candidates"]}
        assert set(c) <= {n for n, _, _ in run_log.SCHEMAS["candidates"]}
    for c in calls:
        assert set(c) <= {n for n, _, _ in run_log.SCHEMAS["llm_calls"]}


def test_the_last_log_record_of_each_qid_wins():
    lines = ['{"qid": "Q1", "status": "failed"}', '{"qid": "Q1", "status": "ok"}', "", '{"qid": "Q2", "status": "ok"}']
    assert {q: r["status"] for q, r in li.last_records(lines).items()} == {"Q1": "ok", "Q2": "ok"}


class FakeTables:
    """Two BigQuery tables in memory. `fail` names a table whose next load raises."""

    def __init__(self, fail=None):
        self.rows = {t: [] for t in li.LOAD_ORDER}
        self.fail = fail

    def existing_ids(self, table):
        return {r[li.ID_FIELD[table]] for r in self.rows[table]}

    def load(self, table, rows):
        if table == self.fail:
            self.fail = None
            raise RuntimeError(f"load job for {table} failed")
        self.rows[table] += rows


def test_a_retry_after_candidates_failed_adds_only_the_missing_rows():
    calls, cands, _ = li.build_university(rec(), FILES, CSV, MODELS)
    rows = {"llm_calls": calls, "candidates": cands}
    db = FakeTables(fail="candidates")
    try:
        li.import_rows(rows, db.existing_ids, db.load)
        raise AssertionError("the candidates load should have failed")
    except RuntimeError:
        pass
    assert len(db.rows["llm_calls"]) == len(calls) and db.rows["candidates"] == []

    added = li.import_rows(rows, db.existing_ids, db.load)
    assert added == {"llm_calls": 0, "candidates": len(cands)}
    for t in li.LOAD_ORDER:
        ids = [r[li.ID_FIELD[t]] for r in db.rows[t]]
        assert len(ids) == len(set(ids)) == len(rows[t])

    # A third run finds everything there and adds nothing.
    assert li.import_rows(rows, db.existing_ids, db.load) == {"llm_calls": 0, "candidates": 0}


def test_missing_rows_drops_ids_already_there_and_repeated_ids():
    rows = [{"candidate_id": "a"}, {"candidate_id": "b"}, {"candidate_id": "b"}, {"candidate_id": "c"}]
    assert [r["candidate_id"] for r in li.missing_rows(rows, "candidate_id", {"a"})] == ["b", "c"]
