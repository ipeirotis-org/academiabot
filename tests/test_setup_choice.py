"""Tests of the week 6 setup-choice numbers (Shuo; SPECS.md, "Shuo, week 6")."""

from wikidata_discover.eval import setup_choice as sc


def _g(gid, providers, status, truth="", uset="nyu", q="S1"):
    return {"university_qid": q, "university_set": uset, "group_id": gid, "providers": providers,
            "status": status, "truth_name": truth}


GROUPS = [_g("g1", "openai; anthropic; gemini", "correct", "Accounting"),
          _g("g2", "gemini", "wrong"),
          _g("g3", "anthropic", "correct", "Finance"),
          _g("g4", "openai; gemini", "correct", "Accounting")]   # a second name of Accounting


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
    assert (gem["kept"], gem["correct"], gem["true_found"]) == (3, 2, 1)   # Accounting found twice counts once
    assert gem["precision"] == round(2 / 3, 4) and gem["recall"] == round(1 / 3, 4)
    assert ("all", "gemini", "named by 2+ of these") not in get
    two = get[("all", "openai + gemini", "named by 2+ of these")]
    assert two["kept"] == 2 and two["precision"] == 1.0
    judged = get[("all", "openai + anthropic + gemini", "anthropic judge (3 of 4 runs)")]
    assert (judged["kept"], judged["precision"], judged["recall"]) == (2, 1.0, round(2 / 3, 4))
    assert get[("nyu", "anthropic", "none")]["true_total"] == 3


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
