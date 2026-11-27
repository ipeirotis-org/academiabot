"""The LLM judge, tested (Shuo, week 4). No network."""

import json

import pytest

import wikidata_discover.llm_helpers as lh
from wikidata_discover.eval import judge as jd
from wikidata_discover.eval.agreement import university_matcher
from wikidata_discover.llm_helpers import LLMHelper

NAMES = ["School of Law", "College of Arts and Science", "School of Maritime Studies"]


# ── the judge call: cache key, cache, retries ─────────────────────────────

def test_order_sample_and_provider_each_change_the_judge_cache_key():
    keys = {lh.judge_cache_key(p, "NYU", names, s)
            for p in ("openai", "anthropic", "gemini")
            for names in (NAMES, NAMES[::-1])
            for s in (1, 2)}
    assert len(keys) == 12


def test_a_one_name_list_gets_a_key_per_order_and_other_keys_stay_as_they_were():
    one = ["School of Law"]
    keys = {lh.judge_cache_key("openai", "NYU", one, s, order=o) for o in ("A", "B") for s in (1, 2)}
    assert len(keys) == 4            # four runs, four calls, even though A and B are the same list
    for names in (NAMES, NAMES[::-1]):
        assert lh.judge_cache_key("openai", "NYU", names, 1, order="B") == lh.judge_cache_key("openai", "NYU", names, 1)


def test_the_judge_cache_key_differs_from_the_extraction_key():
    assert lh.judge_cache_key("openai", "NYU", NAMES, 1) != lh.extract_cache_key("openai", "NYU", "", 1)


@pytest.mark.parametrize("bad", [0, -1, None, True, "1"])
def test_a_bad_judge_sample_is_refused(bad):
    with pytest.raises(ValueError):
        lh.judge_cache_key("openai", "NYU", NAMES, bad)


@pytest.mark.parametrize("text, kept", [
    ('{"keep": ["School of Law"]}', ["School of Law"]),
    ('```json\n{"keep": []}\n```', []),
])
def test_parse_judge_answer(text, kept):
    assert lh.parse_judge_answer(text) == kept


@pytest.mark.parametrize("text", [None, "", "no json", '{"keep": "School of Law"}', '{"keep": [1]}', '{"other": []}'])
def test_parse_judge_answer_refuses_a_bad_answer(text):
    with pytest.raises(ValueError):
        lh.parse_judge_answer(text)


def fake_judge(monkeypatch, tmp_path, answers):
    """_judge_request returns the next text of `answers` per call. Returns the call list."""
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    calls = []

    def request(provider, model, prompt):
        calls.append((provider, prompt))
        return object(), answers[len(calls) - 1]
    monkeypatch.setattr(lh, "_judge_request", request)
    return calls


def test_four_runs_are_four_calls_and_a_rerun_replays_them(monkeypatch, tmp_path):
    """Check 2 of the spec: each (order, sample) is its own call, and a rerun reads the cache."""
    answers = [json.dumps({"keep": NAMES[:k]}) for k in (1, 2, 3, 0)]
    calls = fake_judge(monkeypatch, tmp_path, answers)
    first = [LLMHelper.judge_sampled("NYU", jd.ordered(NAMES, o), "openai", s, o) for o, s in jd.RUNS]
    assert len(calls) == 4
    assert len(list(tmp_path.glob("*.json"))) == 4
    again = [LLMHelper.judge_sampled("NYU", jd.ordered(NAMES, o), "openai", s, o) for o, s in jd.RUNS]
    assert len(calls) == 4 and again == first


def test_the_prompt_lists_the_names_in_the_order_given(monkeypatch, tmp_path):
    calls = fake_judge(monkeypatch, tmp_path, ['{"keep": []}'] * 2)
    LLMHelper.judge_sampled("NYU", jd.ordered(NAMES, "A"), "anthropic", 1, "A")
    LLMHelper.judge_sampled("NYU", jd.ordered(NAMES, "B"), "anthropic", 1, "B")
    a, b = calls[0][1], calls[1][1]
    assert a.index("Maritime") > a.index("Law")
    assert b.index("Maritime") < b.index("Law")


def test_a_bad_answer_is_retried_and_never_cached(monkeypatch, tmp_path):
    calls = fake_judge(monkeypatch, tmp_path, ["not json", '{"keep": ["School of Law"]}'])
    assert LLMHelper.judge_sampled("NYU", NAMES, "gemini", 1) == ["School of Law"]
    assert len(calls) == 2


def test_no_valid_answer_returns_none_and_caches_nothing(monkeypatch, tmp_path):
    calls = fake_judge(monkeypatch, tmp_path, ["", "not json"])
    assert LLMHelper.judge_sampled("NYU", NAMES, "gemini", 1) is None
    assert len(calls) == lh._EXTRACT_MAX_RETRIES
    assert not list(tmp_path.glob("*.json"))


def test_an_empty_list_needs_no_call(monkeypatch, tmp_path):
    calls = fake_judge(monkeypatch, tmp_path, [])
    assert LLMHelper.judge_sampled("NYU", [], "openai", 1) == []
    assert calls == []


# ── orders and mapping the answer back to the list ───────────────────────

def test_order_b_is_order_a_reversed():
    a = jd.ordered(["b", "C", "a"], "A")
    assert a == ["a", "b", "C"]
    assert jd.ordered(["b", "C", "a"], "B") == a[::-1]
    with pytest.raises(ValueError):
        jd.ordered(NAMES, "C")


def test_map_kept_exact_case_and_variant_names():
    match = university_matcher("New York University")
    names = ["School of Law", "College of Arts and Science", "Stern School of Business"]
    taken, unmapped = jd.map_kept(["school of law", "Stern School of Business", "NYU College of Arts & Science",
                                   "School of Medicine"], names, match)
    assert taken == {0: "school of law", 2: "Stern School of Business", 1: "NYU College of Arts & Science"}
    assert unmapped == ["School of Medicine"]


def test_a_name_returned_twice_takes_one_entry():
    taken, unmapped = jd.map_kept(["School of Law", "School of Law"], ["School of Law"], lambda a, b: a == b)
    assert taken == {0: "School of Law"} and unmapped == ["School of Law"]


# ── the analysis on made-up verdicts ─────────────────────────────────────

def verdict(group, judge, order, sample, kept, status="correct", providers=("openai",), uset="eval", qid="Q1"):
    return {"university_qid": qid, "university_set": uset, "group_id": group, "status": status,
            "providers": list(providers), "n_providers": len(providers), "judge": judge,
            "order": order, "sample": sample, "kept": kept}


def test_flip_rates_same_order_and_cross_order():
    # g1 flips only between orders, g2 flips inside order A.
    vs = [verdict("g1", "openai", "A", 1, 1), verdict("g1", "openai", "A", 2, 1),
          verdict("g1", "openai", "B", 1, 0), verdict("g1", "openai", "B", 2, 0),
          verdict("g2", "openai", "A", 1, 1), verdict("g2", "openai", "A", 2, 0),
          verdict("g2", "openai", "B", 1, 1), verdict("g2", "openai", "B", 2, 1)]
    row = next(r for r in jd.flip_rates(vs, ["openai"]) if r["judge"] == "openai" and r["university_set"] == "all")
    assert (row["same_order_pairs"], row["same_order_flips"]) == (4, 1)
    assert (row["cross_order_pairs"], row["cross_order_flips"]) == (8, 6)
    assert row["same_order_flip_rate"] == 0.25 and row["cross_order_flip_rate"] == 0.75
    assert row["order_effect"] == 0.5


def test_a_run_without_an_answer_is_left_out_of_the_pairs():
    vs = [verdict("g1", "openai", "A", 1, 1), verdict("g1", "openai", "A", 2, ""),
          verdict("g1", "openai", "B", 1, 1), verdict("g1", "openai", "B", 2, 1)]
    row = next(r for r in jd.flip_rates(vs, ["openai"]) if r["judge"] == "openai" and r["university_set"] == "all")
    assert row["same_order_pairs"] == 1 and row["cross_order_pairs"] == 2


def test_self_preference_compares_at_the_same_correctness():
    vs = [verdict("w1", "openai", "A", 1, 1, "wrong", ("openai",)),
          verdict("w2", "openai", "A", 1, 0, "wrong", ("gemini",)),
          verdict("c1", "openai", "A", 1, 1, "correct", ("openai", "gemini")),
          verdict("c2", "openai", "A", 1, 1, "correct", ("gemini",))]
    rows = jd.self_preference(vs, ["openai"])
    wrong = next(r for r in rows if r["scope"] == "all" and r["status"] == "wrong")
    assert (wrong["own_keep_rate"], wrong["other_keep_rate"], wrong["difference"]) == (1.0, 0.0, 1.0)
    single = next(r for r in rows if r["scope"] == "one_provider" and r["status"] == "correct")
    assert single["own_verdicts"] == 0 and single["other_verdicts"] == 1


def test_precision_recall_before_and_after_a_judge():
    groups = [{"university_qid": "Q1", "university_set": "eval", "group_id": g, "status": s, "n_providers": n}
              for g, s, n in (("c1", "correct", 3), ("c2", "correct", 1), ("w1", "wrong", 2), ("w2", "wrong", 1))]
    vs = []
    for order, sample in jd.RUNS:
        vs += [verdict("c1", "openai", order, sample, 1), verdict("c2", "openai", order, sample, 0),
               verdict("w1", "openai", order, sample, 1 if sample == 1 else 0),
               verdict("w2", "openai", order, sample, 0)]
    rows = jd.precision_recall(groups, vs, ["openai"], {"Q1": 4})
    get = lambda m, r: next(x for x in rows if x["method"] == m and x["run"] == r and x["university_set"] == "all")
    assert get("no_filter", "")["precision"] == 0.5 and get("no_filter", "")["recall"] == 0.5
    assert get("agreement_2plus", "")["schools_kept"] == 2
    a1 = get("judge_openai", "A1")
    assert (a1["correct_kept"], a1["wrong_kept"], a1["recall"]) == (1, 1, 0.25)
    assert get("judge_openai", "A2")["precision"] == 1.0
    assert get("judge_openai", "mean")["precision"] == 0.75
    maj = get("judge_openai", "majority_3_of_4")
    assert maj["schools_kept"] == 1 and maj["share_of_wrong_removed"] == 1.0


def test_a_university_with_no_groups_still_counts_in_its_set_recall():
    # Q1 found 1 of its 1 true school; Q2 (9 true schools) gave no list at all.
    groups = [{"university_qid": "Q1", "university_set": "nyu", "group_id": "c1", "status": "correct",
               "n_providers": 3}]
    counts = {"Q1": 1, "Q2": 9}
    rows = jd.precision_recall(groups, [], [], counts, {"Q1": "nyu", "Q2": "nyu"})
    get = lambda uset: next(x for x in rows if x["method"] == "no_filter" and x["university_set"] == uset)
    assert get("nyu")["recall"] == 0.1 and get("all")["recall"] == 0.1
    # A set whose only university gave no groups still gets its rows, with recall 0.
    rows = jd.precision_recall(groups, [], [], counts, {"Q1": "nyu", "Q2": "other"})
    other = next(x for x in rows if x["method"] == "no_filter" and x["university_set"] == "other")
    assert (other["schools_kept"], other["recall"]) == (0, 0.0)


def test_a_kept_duplicate_counts_when_the_first_group_of_its_school_is_dropped():
    # Two groups name the same true school: c1 matched it, d1 is the duplicate.
    groups = [{"university_qid": "Q1", "university_set": "eval", "group_id": "c1", "status": "correct",
               "n_providers": 2, "truth_name": "Law", "truth_options": ["Law"]},
              {"university_qid": "Q1", "university_set": "eval", "group_id": "d1", "status": "wrong",
               "n_providers": 1, "truth_name": "Law", "truth_options": ["Law"]}]
    assert jd.matched_count(groups) == 1 and jd.matched_count(groups[1:]) == 1
    vs = []
    for order, sample in jd.RUNS:
        vs += [verdict("c1", "openai", order, sample, 0), verdict("d1", "openai", order, sample, 1)]
    rows = jd.precision_recall(groups, vs, ["openai"], {"Q1": 1})
    a1 = next(x for x in rows if x["method"] == "judge_openai" and x["run"] == "A1" and x["university_set"] == "all")
    assert (a1["correct_kept"], a1["wrong_kept"], a1["precision"], a1["recall"]) == (1, 0, 1.0, 1.0)
    nf = next(x for x in rows if x["method"] == "no_filter" and x["university_set"] == "all")
    assert (nf["correct_kept"], nf["wrong_kept"]) == (1, 1)     # both kept: the school once, one wrong


def test_the_week3_groups_carry_every_true_school_they_can_match():
    from wikidata_discover.eval import agreement as ag
    props = [{"provider": "openai", "sample": 1, "name": "School of Law", "unit_type": "school"},
             {"provider": "gemini", "sample": 1, "name": "Law School", "unit_type": "school"}]
    gs = ag.analyze_university("Q1", props, ["School of Law", "School of Medicine"], [("openai", 1), ("gemini", 1)],
                               match=lambda a, b: "Law" in a and "Law" in b)
    assert all(g["truth_options"] == ["School of Law"] for g in gs)


# ── check 1: a hand-made list with fake schools ──────────────────────────

def test_hand_list_reports_each_fake_school_removed(tmp_path):
    path = tmp_path / "hand.csv"
    path.write_text("university_qid,name,fake\nQ1,School of Law,no\nQ1,School of Maritime Studies,yes\n"
                    "Q1,College of Arts and Science,\nQ1,Jet Propulsion Laboratory,wrong\n")
    hand = jd.read_hand_list(path)
    assert hand == {"Q1": [("School of Law", "no"), ("School of Maritime Studies", "yes"),
                           ("College of Arts and Science", "no"), ("Jet Propulsion Laboratory", "wrong")]}
    good = lambda label, names, judge, sample, order: [n for n in names if "Maritime" not in n and "Jet" not in n]
    lazy = lambda label, names, judge, sample, order: list(names)
    rows = jd.judge_hand_list(hand, {"Q1": {"name": "NYU"}}, ["openai"], good)
    rows += jd.judge_hand_list(hand, {"Q1": {"name": "NYU"}}, ["gemini"], lazy)
    s = {r["judge"]: r for r in jd.hand_summary(rows)}
    assert s["openai"]["removed_every_fake_every_run"] and s["openai"]["fake_removed"] == 4
    assert s["openai"]["real_kept"] == 8 and s["openai"]["known_wrong_removed"] == 4
    assert all(r["correct_verdict"] == 1 for r in rows if r["judge"] == "openai")
    assert s["gemini"]["known_wrong_kept_at_least_once"] == ["Jet Propulsion Laboratory"]
    assert not s["gemini"]["removed_every_fake_every_run"]
    assert s["gemini"]["fakes_kept_at_least_once"] == ["School of Maritime Studies"]


def test_an_unanswered_run_does_not_pass_the_every_fake_removed_check():
    hand = {"Q1": [("School of Law", "no"), ("School of Maritime Studies", "yes")]}
    calls = []

    def one_answer(label, names, judge, sample, order):     # only A1 answers; 3 runs give no answer
        calls.append(1)
        return ["School of Law"] if len(calls) == 1 else None
    rows = jd.judge_hand_list(hand, {"Q1": {"name": "NYU"}}, ["openai"], one_answer)
    s = jd.hand_summary(rows)[0]
    assert s["fake_removed"] == 1 and s["fake_verdicts"] == 1
    assert not s["removed_every_fake_every_run"]


@pytest.mark.parametrize("content", ['{"keep": ["School of La', '{"kept": []}', "[]"])
def test_cache_only_never_calls_a_judge_on_a_broken_cache_file(monkeypatch, tmp_path, content):
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(LLMHelper, "judge_sampled", staticmethod(lambda *a, **k: pytest.fail("must not call")))
    key = lh.judge_cache_key("openai", "NYU", NAMES, 1)
    (tmp_path / f"{key}.json").write_text(content)
    assert jd.cached_ask(True)("NYU", NAMES, "openai", 1, "A") is None


def test_judge_university_marks_a_silent_run_unclear():
    u = {"qid": "Q1", "name": "NYU", "set": "eval"}
    groups = [{"group_id": "Q1-g01", "name": "School of Law", "status": "correct", "providers": ["openai"],
               "n_providers": 1},
              {"group_id": "Q1-g02", "name": "Career Office", "status": "wrong", "providers": ["gemini"],
               "n_providers": 1}]

    def ask(label, names, judge, sample, order):
        return None if (order, sample) == ("B", 2) else ["School of Law"]
    lists, verdicts, runs = jd.judge_university(u, groups, ["openai"], ask)
    assert len(lists) == 4 and len(verdicts) == 8 and len(runs) == 4
    assert [r["answered"] for r in runs] == [True, True, True, False]
    law = [v["kept"] for v in verdicts if v["group_id"] == "Q1-g01"]
    assert law == [1, 1, 1, ""]


# ── sample numbers for the department step (subunits --sample) ───────────

def subunit_key(monkeypatch, sample):
    """The cache key _subunits_from asks for, with the given SUBUNIT_SAMPLE (no call is made)."""
    seen = []
    monkeypatch.setattr(lh, "SUBUNIT_SAMPLE", sample)
    monkeypatch.setattr(lh, "_load_cache", lambda key: seen.append(key) or [])
    lh._subunits_from("anthropic", {"qid": "Q770467", "label": "Stern School of Business"}, "single")
    return seen[0]


def test_subunit_samples_have_their_own_cache_keys(monkeypatch):
    keys = {subunit_key(monkeypatch, s) for s in (None, 1, 2, 3)}
    assert len(keys) == 4


def test_no_subunit_sample_keeps_the_old_cache_key(monkeypatch):
    """Answers cached before samples existed must still be found."""
    unit = {"qid": "Q770467", "label": "Stern School of Business"}
    request = {"tools": lh.subunit_tools("anthropic"), "max_tokens": lh.SUBUNIT_MAX_TOKENS,
               "max_searches": lh.SUBUNIT_MAX_SEARCHES, "search_forced": False}
    old = lh._cache_key(unit["label"], "anthropic", lh.config.ANTHROPIC_MODEL, purpose="subunits",
                        prompt_hash=lh.SUBUNIT_PROMPT_HASH,
                        extra=json.dumps({"qid": unit["qid"], "input": lh.subunit_input(unit), **request}, sort_keys=True))
    assert subunit_key(monkeypatch, None) == old


def test_cli_refuses_a_bad_sample(monkeypatch):
    import sys
    from wikidata_discover import cli
    monkeypatch.setattr(sys, "argv", ["prog", "subunits", "Q770467", "--sample", "0"])
    with pytest.raises(SystemExit):
        cli.run_cli()


# ── departments ───────────────────────────────────────────────────────────

def write_list(folder, qid, provider, sample, names):
    path = jd.dept_list_path(folder, qid, provider, sample)
    path.write_text("name,unit_type\n" + "".join(f"{n},department\n" for n in names))


def test_department_lists_are_read_scored_merged_and_judged(tmp_path):
    from wikidata_discover.eval import dept_scorer as ds
    truth = ds.load_ground_truth(jd.DEPT_TRUTH)
    school = next(s for s in jd.load_dept_schools() if s["parent"] == "Courant")
    assert school["set"] == "nyu" and school["school_qid"] == "Q946028"
    write_list(tmp_path, "Q946028", "openai", 1, ["Department of Mathematics", "Computer Science"])
    write_list(tmp_path, "Q946028", "anthropic", 1, ["Mathematics", "Department of Underwater Basketry"])
    props, runs = jd.read_dept_lists(tmp_path, school, ["openai", "anthropic", "gemini"], 1)
    assert [r["answered"] for r in runs] == [True, True, False]
    scores = {r["provider"]: r for r in jd.score_dept_lists(truth, school, props, runs)}
    assert scores["openai"]["name_precision"] == 1.0
    assert scores["anthropic"]["name_precision"] == 0.5
    assert scores["gemini"]["name_f1"] == ""
    groups = jd.dept_groups(truth, school, props, runs)
    by_status = {g["name"]: g["status"] for g in groups}
    assert len(groups) == 3                                  # "Department of Mathematics" and "Mathematics" merge
    assert by_status["Department of Underwater Basketry"] == "wrong"
    assert sum(1 for s in by_status.values() if s == "correct") == 2

    def ask(label, names, judge, sample, order):
        assert label.startswith("Courant")
        return [n for n in names if "Basketry" not in n]
    lists, verdicts, jruns = jd.judge_university({"qid": "Q946028", "name": "Courant, NYU", "set": "nyu"},
                                                 groups, ["openai"], ask, level="department",
                                                 match=ds.dept_names_match)
    assert all(v["kept"] == (0 if "Basketry" in v["name"] else 1) for v in verdicts)


def test_every_department_school_is_in_the_ground_truth():
    from wikidata_discover.eval import dept_scorer as ds
    parents = {t.parent for t in ds.load_ground_truth(jd.DEPT_TRUTH).rows}
    assert {s["parent"] for s in jd.load_dept_schools()} <= parents


def test_compare_dirs_finds_a_changed_or_missing_file(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "departments").mkdir(parents=True)
        for f in jd.COMPARED_FILES:
            (d / f).write_text("x")
        for f in jd.DEPT_COMPARED_FILES:
            (d / "departments" / f).write_text("y")
    assert jd.compare_dirs(a, b) == []
    (b / "flip_rates.csv").write_text("z")
    (b / "departments" / "groups.csv").unlink()
    assert jd.compare_dirs(a, b) == ["flip_rates.csv", "departments/groups.csv"]


def test_the_department_judge_has_its_own_prompt_and_key():
    assert lh.judge_cache_key("openai", "Stern", NAMES, 1, "department") != lh.judge_cache_key("openai", "Stern", NAMES, 1)
    assert "departments" in lh.DEPT_JUDGE_PROMPT_TEMPLATE and "SCHOOL" in lh.DEPT_JUDGE_PROMPT_TEMPLATE


def test_a_school_with_no_item_is_found_by_its_no_item_files(tmp_path):
    school = {"school_qid": "none", "school_label": "Point Park University Conservatory of Performing Arts",
              "university_qid": "Q7208107"}
    assert jd.school_stem(school) == "noitem-Q7208107-point-park-university-conservatory-of-performing-arts"
    assert jd.school_stem({"school_qid": "Q770467", "school_label": "Stern"}) == "Q770467"


def test_two_no_item_schools_with_one_name_read_their_own_lists(tmp_path):
    a = {"school_qid": "none", "school_label": "School of Business", "university_qid": "Q1"}
    b = {"school_qid": "none", "school_label": "School of Business", "university_qid": "Q2"}
    assert jd.school_stem(a) != jd.school_stem(b)
    for school, dept in ((a, "Accounting"), (b, "Marketing")):
        jd.dept_list_path(tmp_path, jd.school_stem(school), "openai", 1).write_text(f"name\n{dept}\n")
    assert [p["name"] for p in jd.read_dept_lists(tmp_path, {**a, "id": jd.school_stem(a)}, ["openai"], 1)[0]] == ["Accounting"]
    assert [p["name"] for p in jd.read_dept_lists(tmp_path, {**b, "id": jd.school_stem(b)}, ["openai"], 1)[0]] == ["Marketing"]


def test_a_no_item_list_written_before_the_parent_was_in_the_name_is_still_read(tmp_path):
    school = {"school_qid": "none", "school_label": "Conservatory of Performing Arts", "university_qid": "Q7208107"}
    (tmp_path / "noitem-conservatory-of-performing-arts_openai_s1_departments.csv").write_text("name\nDance\n")
    props, runs = jd.read_dept_lists(tmp_path, {**school, "id": jd.school_stem(school)}, ["openai"], 1)
    assert [p["name"] for p in props] == ["Dance"] and runs[0]["file_exists"]


def test_a_failed_week3_lookup_still_writes_the_runs_row(monkeypatch, tmp_path):
    from wikidata_discover import batch, cli, config, run_log
    finished = []

    class Log:
        def attach_source_patch(self, *a): pass
        def finish(self, outcome, summary):
            finished.append(outcome)
            return {}

    def lookup_fails():
        raise RuntimeError("BigQuery unreachable")

    monkeypatch.setattr(config, "load_missing_keys", lambda: {})
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test")
    monkeypatch.setattr(jd.agreement, "universities_to_run",
                        lambda q: [{"qid": "Q1", "name": "NYU", "website": "", "set": "eval", "schools": []}])
    monkeypatch.setattr(jd, "merged_groups", lambda *a: [])
    monkeypatch.setattr(jd, "week3_agreement_checks", lookup_fails)
    monkeypatch.setattr(batch, "source_identity", lambda d: "abc")
    monkeypatch.setattr(batch, "operator_identity", lambda: "tester")
    monkeypatch.setattr(cli, "run_config", lambda k: {})
    log = Log()
    monkeypatch.setattr(run_log, "start", lambda *a, **k: log)
    monkeypatch.setattr(run_log, "current", lambda: log)
    monkeypatch.setattr(run_log, "describe", lambda status: "")
    with pytest.raises(RuntimeError):
        jd.main(["--judges", "openai", "--out", str(tmp_path / "out")])
    assert finished == ["failed"]
