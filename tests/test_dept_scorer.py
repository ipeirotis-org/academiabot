"""Tests for the department loader and scorer (Shuo, week 2).

The fixture is made up. Its QIDs are not real items, and Steinhardt has an
"Economics" department only so that one name exists under two schools.
The five TestCheck classes are the "You check it by" steps in TASKS.md.
"""

from pathlib import Path

import pytest

from wikidata_discover.eval import dept_scorer as ds

FIXTURE = Path(__file__).parent / "fixtures" / "dept_truth_sample.csv"
STERN = "Stern School of Business"
COURANT = "Courant Institute of Mathematical Sciences"
STEINHARDT = "Steinhardt School of Culture, Education, and Human Development"


@pytest.fixture
def truth():
    return ds.load_ground_truth(FIXTURE)


def as_records(truth, with_parent=True, with_qid=True):
    """The ground truth itself, as a proposed list."""
    out = []
    for t in truth.rows:
        rec = {"name": t.name}
        if with_parent:
            rec["parent"] = t.parent
        if with_qid:
            rec["qid"] = t.qid if t.qid != ds.NEW else "none"
        out.append(rec)
    return out


def run(truth, records, **kw):
    return ds.score(truth, ds.parse_proposed(records), **kw).metrics


def test_fixture_loads(truth):
    assert len(truth.rows) == 10
    assert truth.warnings == []
    assert sum(1 for t in truth.rows if t.qid == ds.NEW) == 3


def test_ground_truth_scores_perfectly(truth):
    m = run(truth, as_records(truth))
    for k in ("name", "edge", "ancestor"):
        assert m[f"{k}_precision"] == m[f"{k}_recall"] == m[f"{k}_f1"] == 1.0
    assert m["alignment_accuracy"] == 1.0


class TestCheck1TwoMadeUpNames:
    def test_recall_full_and_exactly_two_wrong(self, truth):
        recs = as_records(truth) + [
            {"name": "Department of Underwater Basket Weaving", "parent": STERN, "qid": ""},
            {"name": "Institute of Time Travel Studies", "parent": COURANT, "qid": ""},
        ]
        m = run(truth, recs)
        n = len(truth.rows)
        assert m["name_recall"] == 1.0
        assert m["n_not_in_answer_key"] == 2
        assert m["name_precision"] == pytest.approx(n / (n + 2))


class TestCheck2RemoveOneName:
    def test_recall_drops_by_one(self, truth):
        recs = as_records(truth)[1:]
        m = run(truth, recs)
        n = len(truth.rows)
        assert m["name_recall"] == pytest.approx((n - 1) / n)
        assert m["name_precision"] == 1.0
        assert m["n_missed"] == 1


class TestCheck3ChangeOneQid:
    def test_alignment_drops_by_one_over_n(self, truth):
        recs = as_records(truth)
        recs[0]["qid"] = "Q9999999"
        m = run(truth, recs)
        n = len(truth.rows)
        assert m["alignment_accuracy"] == pytest.approx(1 - 1 / n)
        assert m["name_recall"] == 1.0

    def test_wrongly_new_and_wrongly_linked(self, truth):
        recs = as_records(truth)
        recs[0]["qid"] = "none"       # truth has Q1000001
        recs[3]["qid"] = "Q1000004"   # truth says "none"
        m = run(truth, recs)
        assert m["alignment_accuracy"] == pytest.approx(8 / 10)

    def test_blank_proposed_qid_means_new(self, truth):
        recs = as_records(truth)
        recs[3]["qid"] = ""  # truth says "none": rightly new
        assert run(truth, recs)["alignment_accuracy"] == 1.0


class TestCheck4RepeatCountsOnce:
    def test_repeat_counts_once_and_lowers_precision(self, truth):
        recs = as_records(truth)
        recs.append(dict(recs[0]))
        m = run(truth, recs)
        n = len(truth.rows)
        assert m["name_recall"] == 1.0
        assert m["n_duplicate"] == 1
        assert m["name_precision"] == pytest.approx(n / (n + 1))

    def test_repeat_in_other_words_also_counts_once(self, truth):
        recs = as_records(truth)
        recs.append({"name": "Finance", "parent": STERN, "qid": "Q1000001"})
        m = run(truth, recs)
        assert m["n_duplicate"] == 1
        assert m["n_found"] == 10


class TestCheck5SameNameTwoSchools:
    def test_both_economics_are_separate_rows(self, truth):
        econ = [t for t in truth.rows if t.name == "Department of Economics"]
        assert {t.parent for t in econ} == {STERN, STEINHARDT}

    def test_one_under_wrong_school(self, truth):
        recs = as_records(truth)
        i = next(k for k, r in enumerate(recs) if r["name"] == "Department of Economics" and r["parent"] == STEINHARDT)
        recs[i]["parent"] = COURANT
        res = ds.score(truth, ds.parse_proposed(recs))
        econ = [d for d in res.details if d["proposed_name"] == "Department of Economics"]
        assert sorted(d["status"] for d in econ) == [ds.FOUND, ds.WRONG_PARENT]
        m = res.metrics
        assert m["name_recall"] == 1.0          # both departments were found
        assert m["edge_precision"] == pytest.approx(9 / 10)  # one parent link is wrong
        assert m["ancestor_f1"] == pytest.approx(19 / 20)    # it keeps the right university

    def test_moved_onto_the_other_economics(self, truth):
        # Both proposed under Stern: one is right, the copy is a repeat, Steinhardt's is missed.
        recs = as_records(truth)
        i = next(k for k, r in enumerate(recs) if r["name"] == "Department of Economics" and r["parent"] == STEINHARDT)
        recs[i]["parent"] = STERN
        m = run(truth, recs)
        assert m["n_duplicate"] == 1
        assert m["n_missed"] == 1


def test_ancestor_example_finance_under_courant(truth):
    recs = as_records(truth)
    recs[0]["parent"] = COURANT  # Finance really is under Stern
    m = run(truth, recs)
    assert m["name_precision"] == m["name_recall"] == 1.0
    assert m["edge_f1"] == pytest.approx(0.9)
    assert m["ancestor_f1"] == pytest.approx(0.95)


def test_department_of_prefix_is_ignored(truth):
    recs = [{"name": "Economics", "parent": STERN}, {"name": "Mathematics Department", "parent": COURANT},
            {"name": "Dept. of Computer Science", "parent": COURANT}]
    m = run(truth, recs)
    assert m["n_found"] == 3
    assert m["edge_precision"] == 1.0


def test_different_departments_do_not_match():
    for a, b in [("Finance", "Marketing"), ("Physics", "Psychology"),
                 ("Computer Science", "Computer Engineering")]:
        assert not ds.dept_names_match(f"Department of {a}", b)


def test_parent_names_match_fuzzily(truth):
    recs = [{"name": "Finance", "parent": "Leonard N. Stern School of Business"}]
    m = run(truth, recs)
    assert m["edge_precision"] == 1.0


def test_parent_not_in_answer_key_is_not_scored(truth):
    recs = as_records(truth) + [{"name": "Department of History", "parent": "College of Arts and Science", "qid": ""}]
    m = run(truth, recs)
    assert m["n_parent_not_in_answer_key"] == 1
    assert m["n_proposed"] == 10
    assert m["name_precision"] == 1.0


def test_list_without_parents_gets_name_scores_only(truth):
    m = run(truth, as_records(truth, with_parent=False, with_qid=False))
    # Without parents the two "Department of Economics" rows are one repeated name.
    assert m["n_duplicate"] == 1
    assert m["name_recall"] == pytest.approx(9 / 10)
    assert m["edge_f1"] is None and m["ancestor_f1"] is None
    assert m["alignment_accuracy"] is None


def test_flat_list_against_one_school(truth):
    recs = [{"name": "Finance"}, {"name": "Marketing"}, {"name": "Economics"}, {"name": "Real Estate"}]
    m = run(truth, recs, parent="Stern")
    assert m["n_true"] == 4
    assert m["n_found"] == 3
    assert m["name_precision"] == pytest.approx(3 / 4)
    assert m["name_recall"] == pytest.approx(3 / 4)


def test_parent_flag_needs_a_list_without_parents(truth):
    with pytest.raises(ds.InputError):
        run(truth, as_records(truth), parent=STERN)


def test_blank_truth_qid_is_left_out_of_alignment():
    gt = ds.parse_ground_truth([
        {"parent": "S", "name": "Finance", "type": "department", "source_url": "https://a", "qid": "Q1"},
        {"parent": "S", "name": "Marketing", "type": "department", "source_url": "https://b", "qid": ""},
    ])
    assert any("qid is empty" in w for w in gt.warnings)
    m = run(gt, [{"name": "Finance", "parent": "S", "qid": "Q1"}, {"name": "Marketing", "parent": "S", "qid": "Q7"}])
    assert m["n_alignment_scored"] == 1
    assert m["n_alignment_not_checked"] == 1
    assert m["alignment_accuracy"] == 1.0


class TestLoader:
    ROW = {"parent": "S", "name": "Finance", "type": "department", "source_url": "https://a", "qid": "Q1"}

    def test_missing_column(self):
        rec = dict(self.ROW)
        del rec["qid"]
        with pytest.raises(ds.InputError, match="missing column"):
            ds.parse_ground_truth([rec])

    def test_bad_qid(self):
        with pytest.raises(ds.InputError, match="not a QID"):
            ds.parse_ground_truth([dict(self.ROW, qid="unknown")])

    def test_none_is_case_insensitive(self):
        assert ds.parse_ground_truth([dict(self.ROW, qid="None")]).rows[0].qid == ds.NEW

    def test_repeated_name_under_one_parent(self):
        with pytest.raises(ds.InputError, match="repeats row 2"):
            ds.parse_ground_truth([self.ROW, dict(self.ROW, name="Dept. of Finance")])

    def test_same_name_under_two_parents_is_allowed(self):
        gt = ds.parse_ground_truth([self.ROW, dict(self.ROW, parent="T")])
        assert len(gt.rows) == 2

    def test_empty_name_and_parent(self):
        with pytest.raises(ds.InputError) as e:
            ds.parse_ground_truth([dict(self.ROW, name="", parent="")])
        assert len(e.value.problems) == 2

    def test_source_url_must_be_web_address(self):
        with pytest.raises(ds.InputError, match="source_url"):
            ds.parse_ground_truth([dict(self.ROW, source_url="stern.nyu.edu")])

    def test_empty_source_url_is_a_warning(self):
        gt = ds.parse_ground_truth([dict(self.ROW, source_url="")])
        assert any("source_url is empty" in w for w in gt.warnings)

    def test_column_names_ignore_case_and_spaces(self, tmp_path):
        p = tmp_path / "gt.csv"
        p.write_text(" Parent ,Name,TYPE,source_url,QID\nS,Finance,department,https://a,Q1\n")
        assert ds.load_ground_truth(p).rows[0].name == "Finance"

    def test_proposed_bad_qid(self):
        with pytest.raises(ds.InputError, match="not a QID"):
            ds.parse_proposed([{"name": "Finance", "qid": "abc"}])


def test_cli_writes_details(tmp_path, truth):
    proposed = tmp_path / "list.csv"
    proposed.write_text("name,parent,qid\nFinance,Stern School of Business,Q1000001\nMade Up,Stern School of Business,\n")
    out = tmp_path / "details.csv"
    assert ds.main(["--truth", str(FIXTURE), "--proposed", str(proposed), "--details", str(out)]) == 0
    text = out.read_text()
    assert "found" in text and "not_in_answer_key" in text and "missed" in text


def test_cli_refuses_bad_truth(tmp_path):
    bad = tmp_path / "gt.csv"
    bad.write_text("parent,name\nS,Finance\n")
    proposed = tmp_path / "list.csv"
    proposed.write_text("name\nFinance\n")
    assert ds.main(["--truth", str(bad), "--proposed", str(proposed)]) == 1


def test_short_parent_name_must_be_unique(truth):
    assert ds._resolve_parent("Stern", sorted({t.parent for t in truth.rows})) == STERN
    assert ds._resolve_parent("School", sorted({t.parent for t in truth.rows})) is None
