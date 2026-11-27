"""Week 7 (Shuo): the review sheet and the reviews table. The three "You check it by" steps
of TASKS.md are test_check1_*, test_check2_*, and test_check3_*."""

import copy
import json
from pathlib import Path

import pytest

from wikidata_discover import review as R
from wikidata_discover import run_log
from wikidata_discover.eval import confidence as conf

FIXTURE = Path(__file__).parent / "fixtures" / "hierarchy_nyu_sample.json"


def hierarchy():
    return conf.load_hierarchy(FIXTURE)


def sheet(tmp_path, review_pass="blind", checks=(), name="sheet.xlsx", h=None, **kw):
    rows, columns, fill = R.build_rows(h or hierarchy(), review_pass, checks, **kw)
    return R.write_sheet(tmp_path / name, rows, columns, fill, "test"), rows, columns


def fill_xlsx(path, answers):
    """Fill rows of a saved xlsx by node_id, as a reviewer would in Excel, and save it."""
    from openpyxl import load_workbook
    wb = load_workbook(path)
    ws = wb["review"]
    header = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        nid = row[header.index("node_id")].value
        for col, value in answers.get(nid, {}).items():
            row[header.index(col)].value = value
    wb.save(path)


def accept_cells(row):
    return {"verdict": "accept", **{f"url_checked_{k}": "yes" for k in (1, 2) if row.get(f"page_{k}_url")}}


def load(path, reviewer, store, pass_problems=False):
    rows = R.read_sheet(path)
    existing = R.read_reviews(local=store)
    new, problems, skipped = R.plan_load(rows, reviewer, existing)
    if not pass_problems:
        assert problems == []
    for r in new:
        r["review_id"] = run_log.new_id()
        r["reviewed_at"] = run_log.now_iso()
    R.append_local(new, store)
    return new, problems, skipped


# ─────────────────────────  THE SHEET  ─────────────────────────

def test_blind_sheet_has_every_exported_value_and_no_checks(tmp_path):
    _, rows, columns = sheet(tmp_path)
    for c in ("name", "aliases", "unit_type", "qid", "alignment_candidates", "country", "country_source",
              "description", "website", "source_url", "parent_1", "parent_1_qid", "parent_1_rank",
              "parent_1_qualifiers", "parent_1_source", "parent_2", "page_1_url", "verdict", "url_checked_1",
              "fix_name", "fix_qid", "fix_parent_1", "fix_parent_1_source", "fix_country_source", "notes",
              "node_version", "shown_hash", "run_id"):
        assert c in columns, c
    assert not [c for c in columns if c.startswith("check_") or c == "confidence"]
    assert len(rows) == 9 and {r["pass"] for r in rows} == {"blind"}


def test_blind_order_is_fixed_by_the_seed():
    """The same seed gives the same order, and the first N rows are the same for any N."""
    a = [r["node_id"] for r in R.build_rows(hierarchy(), "blind")[0]]
    b = [r["node_id"] for r in R.build_rows(hierarchy(), "blind")[0]]
    c = [r["node_id"] for r in R.build_rows(hierarchy(), "blind", limit=4)[0]]
    d = [r["node_id"] for r in R.build_rows(hierarchy(), "blind", seed=1)[0]]
    assert a == b and c == a[:4] and d != a
    assert a != sorted(a)


def test_shown_sheet_puts_doubtful_rows_first():
    h = hierarchy()
    good = {"url": "https://cas.nyu.edu/ewp", "outcome": "ok", "status": 200, "final_url": "", "detail": "",
            "title": "", "name_check": "done", "text": "Expository Writing Program, College of Arts and Science",
            "evidence_id": "e"}
    checks = []
    for n in conf.reviewable_nodes(h):
        p = good if n["node_id"] == "n00224" else None
        checks += conf.node_checks(n, p, ["nyu.edu"], lambda u, *a: {"verdict": "supported", "quote": u,
                                                                     "llm_call_ids": []})
    rows = R.build_rows(h, "shown", checks)[0]
    levels = [r["confidence"] for r in rows]
    assert levels == sorted(levels, key=conf.LEVELS.index)
    assert rows[-1]["node_id"] == "n00224" and rows[-1]["confidence"] == "high"
    assert all(r["check_page_status"] for r in rows)


def test_parent_link_source_is_the_unit_page():
    rows = R.build_rows(hierarchy(), "blind")[0]
    shanghai = next(r for r in rows if r["node_id"] == "n00022")
    assert shanghai["parent_1_source"] == shanghai["page_1_url"] == shanghai["parent_2_source"]
    assert shanghai["parent_2_qid"] == "Q2034790"


# ─────────────────────────  CHECK 1: REJECT, FIX, SAVE, REOPEN  ─────────────────────────

def test_check1_verdicts_and_corrections_survive_save_and_reopen(tmp_path):
    path, rows, _ = sheet(tmp_path)
    ids = [r["node_id"] for r in rows]
    by_id = {r["node_id"]: r for r in rows}
    rejects, fix_name, fix_parent = ids[:3], ids[3], ids[4]
    answers = {nid: {"verdict": "reject", "notes": f"not a real unit {i}"} for i, nid in enumerate(rejects)}
    answers[fix_name] = {**{k: "yes" for k in ("url_checked_1",) if by_id[fix_name]["page_1_url"]},
                         "verdict": "fix", "fix_name": "Corrected Name", "url_checked_2":
                         "no" if by_id[fix_name]["page_2_url"] else None}
    answers[fix_parent] = {"verdict": "fix", "fix_parent_1": "College of Arts and Science",
                           "fix_parent_1_qid": "Q5146469", "fix_parent_1_source": "https://cas.nyu.edu/about",
                           "fix_qid": "Q123", "url_checked_1": "no" if by_id[fix_parent]["page_1_url"] else None,
                           "url_checked_2": "yes" if by_id[fix_parent]["page_2_url"] else None}
    fill_xlsx(path, answers)
    reopened = {r["node_id"]: r for r in R.read_sheet(path)}          # save and reopen
    for nid, cells in answers.items():
        for col, value in cells.items():
            assert reopened[nid][col] == (value or ""), (nid, col)
    store = tmp_path / "reviews.jsonl"
    new, _, _ = load(path, "shuo", store)
    assert len(new) == 5
    saved = {r["node_id"]: r for r in R.read_reviews(local=store)}
    assert sorted(saved) == sorted(answers)
    assert [saved[n]["verdict"] for n in rejects] == ["reject"] * 3
    assert saved[fix_name]["corrections"] == {"name": "Corrected Name"}
    assert saved[fix_parent]["corrections"] == {"parent_1": "College of Arts and Science", "parent_1_qid": "Q5146469",
                                                "parent_1_source": "https://cas.nyu.edu/about", "qid": "Q123"}
    assert saved[rejects[0]]["notes"] == "not a real unit 0"
    for n, r in saved.items():
        assert r["node_version"] == by_id[n]["node_version"] and r["pass"] == "blind"
        assert r["run_id"] == "discover-20261118T192056Z-01d646" and r["reviewer"] == "shuo"
    exported = R.export_rows(R.read_reviews(local=store))
    assert len(exported) == 5 and json.loads(next(e for e in exported if e["node_id"] == fix_name)["corrections"]) \
        == {"name": "Corrected Name"}


# ─────────────────────────  CHECK 2: A SECOND REVIEWER  ─────────────────────────

def test_check2_second_reviewer_is_a_new_row(tmp_path):
    path, rows, _ = sheet(tmp_path)
    first = rows[0]
    fill_xlsx(path, {first["node_id"]: accept_cells(first)})
    store = tmp_path / "reviews.jsonl"
    load(path, "shuo", store)
    load(path, "panos", store)
    saved = [r for r in R.read_reviews(local=store) if r["node_id"] == first["node_id"]]
    assert sorted(r["reviewer"] for r in saved) == ["panos", "shuo"]
    assert len({r["review_id"] for r in saved}) == 2
    status = {s["node_id"]: s for s in R.status_rows(hierarchy(), R.read_reviews(local=store))}
    assert status[first["node_id"]]["blind"] == "panos: accept; shuo: accept"


def test_loading_the_same_sheet_twice_adds_nothing(tmp_path):
    path, rows, _ = sheet(tmp_path)
    fill_xlsx(path, {rows[0]["node_id"]: accept_cells(rows[0])})
    store = tmp_path / "reviews.jsonl"
    assert len(load(path, "shuo", store)[0]) == 1
    new, _, skipped = load(path, "shuo", store)
    assert new == [] and skipped == 1
    assert len(R.read_reviews(local=store)) == 1


def test_a_changed_mind_is_a_new_row_and_the_old_one_stays(tmp_path):
    path, rows, _ = sheet(tmp_path)
    nid = rows[0]["node_id"]
    fill_xlsx(path, {nid: accept_cells(rows[0])})
    store = tmp_path / "reviews.jsonl"
    load(path, "shuo", store)
    fill_xlsx(path, {nid: {"verdict": "reject", "notes": "second look"}})
    load(path, "shuo", store)
    saved = R.read_reviews(local=store)
    assert [r["verdict"] for r in saved] == ["accept", "reject"]


# ─────────────────────────  CHECK 3: A LATER RUN CHANGES THE SOURCE URL  ─────────────────────────

def test_check3_changed_source_url_makes_the_unit_unreviewed(tmp_path):
    path, rows, _ = sheet(tmp_path)
    fill_xlsx(path, {r["node_id"]: accept_cells(r) for r in rows[:3]})
    store = tmp_path / "reviews.jsonl"
    load(path, "shuo", store)
    reviews = R.read_reviews(local=store)
    before = {s["node_id"]: s["status"] for s in R.status_rows(hierarchy(), reviews)}
    target = rows[0]["node_id"]
    assert before[target] == "reviewed"

    later = copy.deepcopy(hierarchy())                       # a later run: one new source URL
    later["run_id"] = "discover-later"
    n = next(x for x in later["nodes"] if x["node_id"] == target)
    n["source_url"] = "https://www.nyu.edu/a-new-page"
    n["run_id"] = "discover-later"
    after = {s["node_id"]: s for s in R.status_rows(later, reviews)}
    assert after[target]["status"] == "unreviewed" and after[target]["older_versions"] == 1
    assert [k for k, s in after.items() if s["status"] == "reviewed"] == [r["node_id"] for r in rows[1:3]]
    assert R.current_reviews(n, reviews) == []

    keep = [k for k, s in after.items() if s["status"] == "unreviewed"]
    again = R.build_rows(later, "blind", node_ids=keep)[0]
    assert target in [r["node_id"] for r in again]
    assert next(r for r in again if r["node_id"] == target)["node_version"] != rows[0]["node_version"]


def test_a_hand_edit_without_a_new_version_does_not_keep_the_verdict(tmp_path):
    """The version is computed from the fields, so a stale stored node_version cannot keep an
    old verdict valid."""
    path, rows, _ = sheet(tmp_path)
    fill_xlsx(path, {rows[0]["node_id"]: accept_cells(rows[0])})
    store = tmp_path / "reviews.jsonl"
    load(path, "shuo", store)
    later = copy.deepcopy(hierarchy())
    n = next(x for x in later["nodes"] if x["node_id"] == rows[0]["node_id"])
    n["name"] = n["name"] + " (renamed)"                      # node_version left as it was
    status = {s["node_id"]: s["status"] for s in R.status_rows(later, R.read_reviews(local=store))}
    assert status[rows[0]["node_id"]] == "unreviewed"


# ─────────────────────────  RULES FOR A VERDICT  ─────────────────────────

def problems_for(tmp_path, cells_for):
    path, rows, _ = sheet(tmp_path)
    row = next(r for r in rows if r["page_1_url"] and r["page_2_url"])
    fill_xlsx(path, {row["node_id"]: cells_for(row)})
    return R.plan_load(R.read_sheet(path), "shuo", [])


def test_accept_needs_yes_for_every_page(tmp_path):
    new, problems, _ = problems_for(tmp_path, lambda r: {"verdict": "accept", "url_checked_1": "yes"})
    assert new == [] and "url_checked yes for every page (page 2)" in problems[0]


def test_accept_with_a_fix_value_is_refused(tmp_path):
    new, problems, _ = problems_for(tmp_path, lambda r: {**accept_cells(r), "fix_name": "Other"})
    assert new == [] and "use fix instead" in problems[0]


def test_fix_needs_yes_or_no_per_page_and_a_url_for_a_parent_link(tmp_path):
    _, problems, _ = problems_for(tmp_path, lambda r: {"verdict": "fix", "fix_parent_1": "X", "url_checked_1": "no"})
    assert any("page 2" in p for p in problems)
    assert any("fix_parent_1_source" in p for p in problems)


def test_fix_country_needs_its_source_and_urls_must_be_urls(tmp_path):
    _, problems, _ = problems_for(tmp_path, lambda r: {"verdict": "fix", "fix_country": "China",
                                                       "fix_source_url": "nyu.edu/page",
                                                       "url_checked_1": "no", "url_checked_2": "no"})
    assert any("fix_country_source" in p for p in problems)
    assert any("fix_source_url must be a URL" in p for p in problems)


def test_fix_with_a_dead_page_marked_no_is_saved(tmp_path):
    new, problems, _ = problems_for(tmp_path, lambda r: {"verdict": "fix", "fix_source_url": "https://www.nyu.edu/x",
                                                         "url_checked_1": "no", "url_checked_2": "yes"})
    assert problems == [] and new[0]["url_checked"][0]["checked"] == "no"
    assert new[0]["corrections"] == {"source_url": "https://www.nyu.edu/x"}


def test_a_changed_shown_value_is_refused(tmp_path):
    new, problems, _ = problems_for(tmp_path, lambda r: {**accept_cells(r), "name": "Edited in place"})
    assert new == [] and "shown value was changed" in problems[0]


def test_bad_verdict_word_is_refused(tmp_path):
    new, problems, _ = problems_for(tmp_path, lambda r: {"verdict": "maybe"})
    assert new == [] and "accept, reject, or fix" in problems[0]


def test_empty_rows_are_not_verdicts(tmp_path):
    path, _, _ = sheet(tmp_path)
    assert R.plan_load(R.read_sheet(path), "shuo", []) == ([], [], 0)


def test_reviewer_name_is_required(tmp_path):
    path, _, _ = sheet(tmp_path)
    assert R.plan_load(R.read_sheet(path), " ", [])[1] == ["give the reviewer's name (--reviewer)"]


# ─────────────────────────  THE TWO PASSES  ─────────────────────────

def shown_checks(h):
    return [c for n in conf.reviewable_nodes(h) for c in conf.node_checks(n, None, ["nyu.edu"], None)]


def test_second_pass_needs_a_first_pass_verdict(tmp_path):
    h = hierarchy()
    path, rows, _ = sheet(tmp_path, "shown", shown_checks(h), name="shown.xlsx")
    fill_xlsx(path, {rows[0]["node_id"]: {"verdict": "reject"}})
    new, problems, _ = R.plan_load(R.read_sheet(path), "shuo", [])
    assert new == [] and "needs a first-pass verdict" in problems[0]


def test_first_pass_after_seeing_the_checks_is_refused(tmp_path):
    h = hierarchy()
    blind, brows, _ = sheet(tmp_path, name="blind.xlsx")
    nid = brows[0]["node_id"]
    fill_xlsx(blind, {nid: {"verdict": "reject"}})
    store = tmp_path / "reviews.jsonl"
    load(blind, "shuo", store)
    shown, _, _ = sheet(tmp_path, "shown", shown_checks(h), name="shown.xlsx")
    fill_xlsx(shown, {nid: {"verdict": "reject", "notes": "after the checks"}})
    new, _, _ = load(shown, "shuo", store)
    assert new[0]["pass"] == "shown"
    fill_xlsx(blind, {nid: {"verdict": "reject", "notes": "again"}})
    new, problems, _ = load(blind, "shuo", store, pass_problems=True)
    assert new == [] and "not blind" in problems[0]
    # Panos has not seen the checks, so his first pass is still blind.
    assert len(load(blind, "panos", store)[0]) == 1


def test_shown_hash_covers_the_checks():
    h = hierarchy()
    blind = {r["node_id"]: r for r in R.build_rows(h, "blind")[0]}
    shown = {r["node_id"]: r for r in R.build_rows(h, "shown", shown_checks(h))[0]}
    for nid in blind:
        assert blind[nid]["shown_hash"] != shown[nid]["shown_hash"]
        assert blind[nid]["node_version"] == shown[nid]["node_version"]


# ─────────────────────────  FILES AND THE TABLE  ─────────────────────────

def test_csv_saved_by_excel_on_windows_is_read(tmp_path):
    path, rows, columns = sheet(tmp_path, name="sheet.csv")
    assert path.read_text(encoding="utf-8").splitlines()[0].split(",") == columns
    back = R.read_sheet(path)
    assert [r["node_id"] for r in back] == [r["node_id"] for r in rows]
    cp = tmp_path / "cp1252.csv"
    cp.write_bytes(path.read_text(encoding="utf-8").replace("Liberal", "Libéral", 1).encode("cp1252"))
    assert R.read_sheet(cp)[0]["node_id"] == rows[0]["node_id"]


def test_reviews_table_schema_and_add_review():
    names = [c[0] for c in run_log.SCHEMAS["reviews"]]
    for c in ("review_id", "node_id", "candidate_id", "parent_id", "run_id", "node_version", "shown_hash",
              "reviewer", "source", "pass", "verdict", "corrections", "url_checked", "notes", "reviewed_at"):
        assert c in names
    assert "reviews" in run_log.TABLES
    log = run_log.RunLog("review-load-test", enabled=False)
    rid = log.add_review({"run_id": "discover-x", "node_id": "n1", "node_version": "v1", "pass": "blind",
                          "verdict": "fix", "corrections": {"name": "A"}, "url_checked": [], "reviewer": "shuo"})
    row = log.pending["reviews"][0]
    assert row["review_id"] == rid and row["run_id"] == "discover-x" and row["load_run_id"] == "review-load-test"
    assert json.loads(row["corrections"]) == {"name": "A"}
    with pytest.raises(ValueError):
        log.add_review({"run_id": "r", "node_version": "v", "pass": "blind", "verdict": "maybe"})
    with pytest.raises(ValueError):
        log.add_review({"run_id": "r", "node_version": "v", "pass": "later", "verdict": "accept"})


def test_shown_cells_do_not_depend_on_the_order_of_the_checks():
    """BigQuery returns rows in any order; the cells and shown_hash must not change."""
    h = hierarchy()
    checks = shown_checks(h)
    a = {r["node_id"]: r["shown_hash"] for r in R.build_rows(h, "shown", checks)[0]}
    b = {r["node_id"]: r["shown_hash"] for r in R.build_rows(h, "shown", list(reversed(checks)))[0]}
    assert a == b
