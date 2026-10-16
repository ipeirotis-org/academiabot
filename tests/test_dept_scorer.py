"""Tests for the department loader and scorer (Shuo, week 2).

The fixture is a copy of Shuo's ground truth for Stern, Courant and Steinhardt
(wikidata_discover/eval/dept_ground_truth.csv). No department name appears under
two schools there, so check 5 adds a made-up "Economics" row under Steinhardt.
The five TestCheck classes are the "You check it by" steps in TASKS.md.
"""

from pathlib import Path

import pytest

from wikidata_discover.eval import dept_scorer as ds

FIXTURE = Path(__file__).parent / "fixtures" / "dept_truth_sample.csv"
N = 24


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


def index_of(recs, name):
    return next(k for k, r in enumerate(recs) if r["name"] == name)


def run(truth, records, **kw):
    return ds.score(truth, ds.parse_proposed(records), **kw).metrics


def with_two_economics(truth):
    """The ground truth plus a made-up Economics under Steinhardt (Stern has the real one)."""
    extra = ds.TrueDept("Steinhardt", "Economics", "academic department", "https://example.org/made-up", ds.NEW)
    return ds.GroundTruth(truth.rows + [extra])


def test_fixture_loads(truth):
    assert len(truth.rows) == N
    assert truth.warnings == []
    assert {t.parent for t in truth.rows} == {"Stern", "Courant", "Steinhardt"}
    assert sum(1 for t in truth.rows if t.qid == ds.NEW) == 13


def test_fixture_matches_the_ground_truth_file():
    real = Path(ds.__file__).parent / "dept_ground_truth.csv"
    assert ds.load_ground_truth(real).rows == ds.load_ground_truth(FIXTURE).rows


def test_ground_truth_scores_perfectly(truth):
    m = run(truth, as_records(truth))
    for k in ("name", "edge", "ancestor"):
        assert m[f"{k}_precision"] == m[f"{k}_recall"] == m[f"{k}_f1"] == 1.0
    assert m["alignment_accuracy"] == 1.0


def test_no_two_true_names_match_each_other(truth):
    names = [t.name for t in truth.rows]
    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1:] if ds.dept_names_match(a, b)]
    assert pairs == []


def test_removing_any_one_row_loses_exactly_that_row(truth):
    for k in range(N):
        recs = [r for j, r in enumerate(as_records(truth)) if j != k]
        assert run(truth, recs)["n_found"] == N - 1


class TestCheck1TwoMadeUpNames:
    def test_recall_full_and_exactly_two_wrong(self, truth):
        recs = as_records(truth) + [
            {"name": "Department of Underwater Basket Weaving", "parent": "Stern", "qid": ""},
            {"name": "Institute of Time Travel Studies", "parent": "Courant", "qid": ""},
        ]
        m = run(truth, recs)
        assert m["name_recall"] == 1.0
        assert m["n_not_in_answer_key"] == 2
        assert m["name_precision"] == pytest.approx(N / (N + 2))


class TestCheck2RemoveOneName:
    def test_recall_drops_by_one(self, truth):
        m = run(truth, as_records(truth)[1:])
        assert m["name_recall"] == pytest.approx((N - 1) / N)
        assert m["name_precision"] == 1.0
        assert m["n_missed"] == 1


class TestCheck3ChangeOneQid:
    def test_alignment_drops_by_one_over_n(self, truth):
        recs = as_records(truth)
        recs[index_of(recs, "Accounting")]["qid"] = "Q9999999"
        m = run(truth, recs)
        assert m["alignment_accuracy"] == pytest.approx(1 - 1 / N)
        assert m["name_recall"] == 1.0

    def test_wrongly_new_and_wrongly_linked(self, truth):
        recs = as_records(truth)
        recs[index_of(recs, "Accounting")]["qid"] = "none"   # truth has Q101027479
        recs[index_of(recs, "Economics")]["qid"] = "Q1"      # truth says "none"
        assert run(truth, recs)["alignment_accuracy"] == pytest.approx((N - 2) / N)

    def test_blank_proposed_qid_means_new(self, truth):
        recs = as_records(truth)
        recs[index_of(recs, "Economics")]["qid"] = ""  # truth says "none": rightly new
        assert run(truth, recs)["alignment_accuracy"] == 1.0


class TestCheck4RepeatCountsOnce:
    def test_repeat_counts_once_and_lowers_precision(self, truth):
        recs = as_records(truth)
        recs.append(dict(recs[index_of(recs, "Finance")]))
        m = run(truth, recs)
        assert m["name_recall"] == 1.0
        assert m["n_duplicate"] == 1
        assert m["name_precision"] == pytest.approx(N / (N + 1))

    def test_repeat_in_other_words_also_counts_once(self, truth):
        recs = as_records(truth)
        recs.append({"name": "Department of Finance", "parent": "Stern", "qid": "Q128602068"})
        m = run(truth, recs)
        assert m["n_duplicate"] == 1
        assert m["n_found"] == N


class TestCheck5SameNameTwoSchools:
    def test_same_name_under_two_schools_is_two_rows(self, truth):
        gt = with_two_economics(truth)
        assert [t.parent for t in gt.rows if t.name == "Economics"] == ["Stern", "Steinhardt"]
        assert run(gt, as_records(gt))["n_found"] == N + 1

    def test_one_under_wrong_school(self, truth):
        gt = with_two_economics(truth)
        recs = as_records(gt)
        recs[-1]["parent"] = "Courant"  # the Steinhardt Economics
        res = ds.score(gt, ds.parse_proposed(recs))
        econ = [d for d in res.details if d["proposed_name"] == "Economics"]
        assert [(d["proposed_parent"], d["status"]) for d in econ] == [
            ("Stern", ds.FOUND), ("Courant", ds.WRONG_PARENT)]
        m = res.metrics
        assert m["name_recall"] == 1.0                          # both were found
        assert m["edge_precision"] == pytest.approx(N / (N + 1))  # one parent link is wrong
        assert m["ancestor_f1"] == pytest.approx((2 * N + 1) / (2 * (N + 1)))

    def test_moved_onto_the_other_economics(self, truth):
        # Both proposed under Stern: one is right, the copy is a repeat, Steinhardt's is missed.
        gt = with_two_economics(truth)
        recs = as_records(gt)
        recs[-1]["parent"] = "Stern"
        m = run(gt, recs)
        assert m["n_duplicate"] == 1
        assert m["n_missed"] == 1


def test_ancestor_example_finance_under_courant(truth):
    recs = as_records(truth)
    recs[index_of(recs, "Finance")]["parent"] = "Courant"
    m = run(truth, recs)
    assert m["name_precision"] == m["name_recall"] == 1.0
    assert m["edge_f1"] == pytest.approx((N - 1) / N)
    assert m["ancestor_f1"] == pytest.approx((2 * N - 1) / (2 * N))


def test_department_of_prefix_is_ignored(truth):
    recs = [{"name": "Department of Accounting", "parent": "Stern"},
            {"name": "Mathematics", "parent": "Courant"},
            {"name": "Dept. of Computer Science", "parent": "Courant"},
            {"name": "Teaching and Learning Department", "parent": "Steinhardt"}]
    m = run(truth, recs)
    assert m["n_found"] == 4
    assert m["edge_precision"] == 1.0


def test_different_departments_do_not_match():
    for a, b in [("Finance", "Marketing"), ("Physics", "Psychology"),
                 ("Computer Science", "Computer Engineering"),
                 ("Occupational Therapy", "Physical Therapy"),
                 ("Applied Psychology", "Applied Statistics, Social Science, and Humanities")]:
        assert not ds.dept_names_match(f"Department of {a}", b)


def test_long_parent_names_find_short_ones(truth):
    recs = [{"name": "Finance", "parent": "Leonard N. Stern School of Business"},
            {"name": "Mathematics", "parent": "Courant Institute of Mathematical Sciences"},
            {"name": "Physical Therapy", "parent": "NYU Steinhardt"}]
    m = run(truth, recs)
    assert m["n_found"] == 3
    assert m["edge_precision"] == 1.0


def test_parent_must_fit_exactly_one_school():
    parents = ["Stern", "Courant", "Steinhardt"]
    assert ds._resolve_parent("Stern School of Business", parents) == "Stern"
    assert ds._resolve_parent("School of Business", parents) is None
    assert ds._resolve_parent("Stern and Courant joint program", parents) is None


def test_parent_not_in_answer_key_is_not_scored(truth):
    recs = as_records(truth) + [{"name": "Department of History", "parent": "College of Arts and Science", "qid": ""}]
    m = run(truth, recs)
    assert m["n_parent_not_in_answer_key"] == 1
    assert m["n_proposed"] == N
    assert m["name_precision"] == 1.0


def test_list_without_parents_gets_name_scores_only(truth):
    m = run(truth, as_records(truth, with_parent=False, with_qid=False))
    assert m["name_recall"] == 1.0
    assert m["edge_f1"] is None and m["ancestor_f1"] is None
    assert m["alignment_accuracy"] is None


def test_without_parents_a_repeated_name_counts_once(truth):
    gt = with_two_economics(truth)
    m = run(gt, as_records(gt, with_parent=False, with_qid=False))
    assert m["n_duplicate"] == 1  # the two Economics rows cannot be told apart
    assert m["name_recall"] == pytest.approx(N / (N + 1))


def test_flat_list_against_one_school(truth):
    recs = [{"name": "Mathematics"}, {"name": "Computer Science"},
            {"name": "Center for Data Science"}, {"name": "Robotics"}]
    m = run(truth, recs, parent="Courant")
    assert m["n_true"] == 3
    assert m["n_found"] == 3
    assert m["name_precision"] == pytest.approx(3 / 4)
    assert m["name_recall"] == 1.0


def test_parent_flag_needs_a_list_without_parents(truth):
    with pytest.raises(ds.InputError):
        run(truth, as_records(truth), parent="Stern")


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

    def test_spaces_in_header_and_cells_are_ignored(self, tmp_path):
        p = tmp_path / "gt.csv"
        p.write_text(" Parent ,name ,TYPE,source_url,QID\nStern ,Finance,department,https://a,Q1\n")
        row = ds.load_ground_truth(p).rows[0]
        assert (row.parent, row.name) == ("Stern", "Finance")

    def test_proposed_bad_qid(self):
        with pytest.raises(ds.InputError, match="not a QID"):
            ds.parse_proposed([{"name": "Finance", "qid": "abc"}])


def test_cli_writes_details(tmp_path):
    proposed = tmp_path / "list.csv"
    proposed.write_text("name,parent,qid\nFinance,Stern,Q128602068\nMade Up,Stern,\n")
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


def test_header_only_file_is_still_checked(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("parent,qid\n")
    with pytest.raises(ds.InputError, match="missing column: name"):
        ds.load_proposed(bad)
    empty = tmp_path / "empty.csv"
    empty.write_text("name,parent,qid\n")
    p = ds.load_proposed(empty)
    assert p.rows == [] and p.has_parents and p.has_qids


def test_parent_that_fuzzy_matches_two_schools_is_not_guessed():
    parents = ["College of Arts and Sciences", "School of Arts and Sciences"]
    assert ds._resolve_parent("Arts and Sciences", parents) is None
    assert ds._resolve_parent("School of Arts and Sciences", parents) == "School of Arts and Sciences"


def test_overlapping_names_still_give_the_most_matches():
    gt = ds.parse_ground_truth([
        {"parent": "S", "name": n, "type": "d", "source_url": "https://a", "qid": "none"}
        for n in ("Chemical Engineering", "Chemical Engineering Studies")])
    recs = [{"name": "Chemical Engineering", "parent": "S"},
            {"name": "Chemical Engineering Analytics", "parent": "S"}]
    assert run(gt, recs)["n_found"] == 2


def test_exact_names_keep_their_pair_when_nothing_conflicts(truth):
    res = ds.score(truth, ds.parse_proposed(as_records(truth)))
    assert all(d["proposed_name"] == d["true_name"] for d in res.details)


def test_row_with_data_but_no_name_is_rejected():
    with pytest.raises(ds.InputError, match="row 3: name is empty"):
        ds.parse_proposed([{"name": "Finance", "parent": "Stern"}, {"name": "", "parent": "Stern"}])
    assert len(ds.parse_proposed([{"name": "Finance", "parent": "Stern"}, {"name": "", "parent": ""}]).rows) == 1
