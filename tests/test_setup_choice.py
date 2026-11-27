"""Tests of the week 6 setup-choice numbers (Shuo; SPECS.md, "Shuo, week 6")."""

from wikidata_discover.eval import setup_choice as sc


def _g(gid, providers, status, truth="", uset="nyu", q="S1", kind=""):
    return {"university_qid": q, "university_set": uset, "group_id": gid, "providers": providers,
            "status": status, "truth_name": truth, "wrong_kind": kind}


GROUPS = [_g("g1", "openai; anthropic; gemini", "correct", "Accounting"),
          _g("g2", "gemini", "wrong"),
          _g("g3", "anthropic", "correct", "Finance"),
          _g("g4", "openai; gemini", "wrong", "Accounting", kind="duplicate")]   # a second name of Accounting


def _verdicts(judge, kept_ids, runs=4):
    return [{"judge": judge, "group_id": g["group_id"], "kept": "1" if g["group_id"] in kept_ids else "0"}
            for g in GROUPS for _ in range(runs)]


def test_generator_sets_cover_every_nonempty_set():
    sets = sc.generator_sets()
    assert len(sets) == 7 and sets[0] == ("openai",) and sets[-1] == ("openai", "anthropic", "gemini")


def test_judge_keeps_needs_3_of_4_runs():
    vs = [{"judge": "openai", "group_id": "g1", "kept": "1"}] * 3 + [{"judge": "openai", "group_id": "g2", "kept": "1"}] * 2
    assert sc.judge_keeps(vs) == {"openai": {"g1"}}


def test_combos_score_each_generator_set_and_filter():
    rows = sc.combos(GROUPS, _verdicts("anthropic", {"g1", "g3"}), {"S1": 3}, "department", judges=["anthropic"])
    get = {(r["set"], r["generators"], r["filter"]): r for r in rows}
    gem = get[("all", "gemini", "none")]
    assert (gem["kept"], gem["correct"], gem["true_found"]) == (3, 1, 1)   # Accounting found twice counts once
    assert gem["precision"] == round(1 / 3, 4) and gem["recall"] == round(1 / 3, 4)
    assert ("all", "gemini", "named by 2+ of these") not in get
    two = get[("all", "openai + gemini", "named by 2+ of these")]
    assert two["kept"] == 2 and two["precision"] == 0.5
    judged = get[("all", "openai + anthropic + gemini", "anthropic judge (3 of 4 runs)")]
    assert (judged["kept"], judged["precision"], judged["recall"]) == (2, 1.0, round(2 / 3, 4))
    assert get[("nyu", "anthropic", "none")]["true_total"] == 3


def test_combos_match_each_kept_subset_again_so_a_kept_duplicate_counts():
    """Review of pull request 29: the judge drops g1 (Accounting) and keeps its duplicate g4,
    so g4 now covers Accounting and counts as correct."""
    rows = sc.combos(GROUPS, _verdicts("anthropic", {"g3", "g4"}), {"S1": 3}, "department", judges=["anthropic"])
    get = {(r["set"], r["generators"], r["filter"]): r for r in rows}
    judged = get[("all", "openai + anthropic + gemini", "anthropic judge (3 of 4 runs)")]
    assert (judged["kept"], judged["correct"], judged["precision"], judged["recall"]) == (2, 2, 1.0, round(2 / 3, 4))
    assert get[("all", "openai", "none")]["correct"] == 1   # g1 and g4 both name Accounting: one match


def test_combos_count_a_requested_university_with_no_groups_in_its_set():
    """Review of pull request 29: S2 gave no list, so its true unit still counts in its set."""
    rows = sc.combos(GROUPS, _verdicts("anthropic", {"g1"}), {"S1": 3, "S2": 1}, "school",
                     judges=["anthropic"], unit_sets={"S1": "nyu", "S2": "nyu"})
    get = {(r["set"], r["generators"], r["filter"]): r for r in rows}
    assert get[("nyu", "anthropic", "none")]["true_total"] == 4
    assert get[("all", "anthropic", "none")]["true_total"] == 4
    rows = sc.combos(GROUPS, [], {"S1": 3, "S9": 2}, "school", judges=[], unit_sets={"S9": "small"})
    assert {(r["set"], r["true_total"]) for r in rows if r["generators"] == "openai"} == {("nyu", 3), ("small", 2), ("all", 5)}


def test_truth_options_column_wins_over_truth_name():
    assert sc.truth_options({"truth_options": "A; B", "truth_name": "A", "status": "correct"}) == ["A", "B"]
    assert sc.truth_options({"truth_name": "", "status": "wrong", "wrong_kind": "invented"}) == []


def test_dept_generators_average_each_providers_lists():
    scores = [{"provider": "openai", "set": "nyu", "answered": "True", "name_precision": "1.0",
               "name_recall": "0.5", "name_f1": "0.6667"},
              {"provider": "openai", "set": "other", "answered": "True", "name_precision": "0.5",
               "name_recall": "1.0", "name_f1": "0.6667"},
              {"provider": "openai", "set": "other", "answered": "False", "name_precision": "",
               "name_recall": "", "name_f1": ""}]
    rows = {(r["provider"], r["set"]): r for r in sc.dept_generators(scores)}
    assert rows[("openai", "all")]["lists"] == 2 and rows[("openai", "all")]["mean_precision"] == 0.75
    assert rows[("anthropic", "all")]["lists"] == 0 and rows[("anthropic", "all")]["mean_f1"] == ""
