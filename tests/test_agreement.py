"""Parallel extraction and agreement (Shuo, week 3). No network."""

import json
from pathlib import Path

import pytest

import wikidata_discover.llm_helpers as lh
from wikidata_discover import run_log
from wikidata_discover.eval import agreement as ag
from wikidata_discover.llm_helpers import LLMHelper


# ── sample numbers in the cache key ──────────────────────────────────────

def test_no_sample_keeps_the_old_cache_key():
    """Answers cached before samples existed must still be found."""
    old = lh._cache_key("NYU", "openai", "gpt-4o", extra="https://www.nyu.edu")
    assert lh._cache_key("NYU", "openai", "gpt-4o", extra=lh._extract_extra("https://www.nyu.edu", None)) == old


def test_each_sample_has_its_own_cache_key():
    keys = {lh._cache_key("NYU", "openai", "gpt-4o", extra=lh._extract_extra("https://www.nyu.edu", s))
            for s in (None, 1, 2, 3)}
    assert len(keys) == 4


@pytest.mark.parametrize("bad", [0, -1, 1.5, "1", True])
def test_a_bad_sample_number_is_refused(bad):
    with pytest.raises(ValueError):
        lh._extract_extra("https://www.nyu.edu", bad)


def fake_anthropic(monkeypatch, tmp_path, answers):
    """Anthropic returns the next answer of `answers` per real call. Returns the call list."""
    import wikidata_discover.config as config
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    calls = []

    class Client:
        def with_options(self, **kw):
            return self

        class messages:
            @staticmethod
            def create(**kw):
                calls.append(kw)
                units = answers[len(calls) - 1]
                text = json.dumps({"units": [{"name": n, "unit_type": "school", "city": "", "state": "",
                                              "website": None} for n in units], "reference": "https://x.edu"})
                return type("M", (), {"content": [type("T", (), {"text": text})()]})()
    monkeypatch.setattr(lh, "_get_anthropic_client", lambda: Client())
    return calls


def test_three_samples_are_three_calls_and_a_rerun_replays_them(monkeypatch, tmp_path):
    calls = fake_anthropic(monkeypatch, tmp_path, [["Law"], ["Law", "Medicine"], ["Business"]])
    first = [LLMHelper.extract_divisions_anthropic("NYU", "https://www.nyu.edu", sample=s) for s in (1, 2, 3)]
    assert len(calls) == 3                                     # three real calls, not one read three times
    assert len(list(tmp_path.glob("*.json"))) == 3             # one cache file per sample
    again = [LLMHelper.extract_divisions_anthropic("NYU", "https://www.nyu.edu", sample=s) for s in (1, 2, 3)]
    assert len(calls) == 3 and again == first                 # the rerun replays the same answers
    assert [[u["name"] for u in a] for a in first] == [["Law"], ["Law", "Medicine"], ["Business"]]


def test_the_sample_number_is_in_the_run_log(monkeypatch, tmp_path):
    fake_anthropic(monkeypatch, tmp_path, [["Law"]])
    log = run_log.RunLog("r", "i", enabled=False, results_dir=tmp_path)
    monkeypatch.setattr(run_log, "_current", log)
    LLMHelper.extract_divisions_anthropic("NYU", "https://www.nyu.edu", sample=2)
    row = log.pending["llm_calls"][0]
    assert json.loads(row["request_config"])["sample"] == 2
    assert row["cache_key"] == lh._cache_key("NYU", "anthropic", lh.config.ANTHROPIC_MODEL,
                                             extra=lh._extract_extra("https://www.nyu.edu", 2))


# ── run log: unaligned candidates and checks ────────────────────────────

def test_checks_rows_follow_the_schema(tmp_path):
    log = run_log.RunLog("r", "i", enabled=False, results_dir=tmp_path)
    cid = log.add_candidate(name="School of Law", status="unaligned", provider="openai")
    chk = log.add_check(kind="agreement", result="pass", candidate_id=cid, score=0.5,
                        llm_call_ids=["c1", "c2"], detail={"providers": ["openai", "gemini"]})
    row = log.pending["checks"][0]
    assert row["check_id"] == chk and row["candidate_id"] == cid and row["kind"] == "agreement"
    assert row["llm_call_ids"] == ["c1", "c2"] and json.loads(row["detail"])["providers"] == ["openai", "gemini"]
    assert set(row) == {n for n, _, _ in run_log.SCHEMAS["checks"]}


@pytest.mark.parametrize("kw", [
    {"kind": "vote", "result": "pass", "candidate_id": "c"},
    {"kind": "agreement", "result": "maybe", "candidate_id": "c"},
    {"kind": "agreement", "result": "pass"},                                   # names no unit
    {"kind": "agreement", "result": "pass", "candidate_id": "c", "node_id": "n"},  # names two
])
def test_a_bad_check_is_refused(tmp_path, kw):
    log = run_log.RunLog("r", "i", enabled=False, results_dir=tmp_path)
    with pytest.raises(ValueError):
        log.add_check(**kw)


def test_checks_go_to_the_local_fallback_too(tmp_path):
    class Broken:
        def load_table_from_json(self, *a, **k):
            raise RuntimeError("no BigQuery")
    log = run_log.RunLog("r", "i", results_dir=tmp_path, bq_client=Broken(), bucket=object())
    cid = log.add_candidate(name="Law", status="unaligned")
    log.add_check(kind="agreement", result="fail", candidate_id=cid)
    log.flush()
    lines = (log.local_dir / "checks.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["candidate_id"] == cid


# ── grouping ─────────────────────────────────────────────────────────────

def P(provider, sample, name, index=0, unit_type="school"):
    return {"provider": provider, "sample": sample, "name": name, "index": index, "unit_type": unit_type,
            "candidate_id": f"{provider}{sample}{name}", "llm_call_id": f"call-{provider}-{sample}"}


def test_name_variants_are_grouped():
    props = [P("openai", 1, "School of Law"), P("gemini", 1, "Law School"),
             P("anthropic", 2, "Stern School of Business"), P("openai", 2, "School of Law")]
    groups = ag.group_names(props)
    names = sorted(sorted(props[i]["name"] for i in g) for g in groups)
    assert names == [["Law School", "School of Law", "School of Law"], ["Stern School of Business"]]


def test_grouping_does_not_depend_on_input_order():
    props = [P("gemini", 1, "Law School"), P("openai", 1, "School of Law"), P("anthropic", 1, "Tisch School of the Arts")]
    a = ag.group_names(props)
    b = ag.group_names(list(reversed(props)))
    as_names = lambda ps, gs: sorted(sorted(ps[i]["name"] for i in g) for g in gs)
    assert as_names(props, a) == as_names(list(reversed(props)), b)


def test_group_label_is_the_most_frequent_name():
    assert ag.group_label(["Law School", "School of Law", "School of Law"]) == "School of Law"
    assert ag.group_label(["B", "A"]) == "B"                                 # first seen on a tie


# ── scoring against the ground truth ─────────────────────────────────────

TRUTH = ["School of Law", "Stern School of Business", "Tisch School of the Arts"]


def analyze(props, answered=None):
    answered = answered or sorted({(p["provider"], p["sample"]) for p in props})
    return ag.analyze_university("Q1", props, TRUTH, answered)


def test_agreement_counts_and_score():
    props = [P("openai", 1, "School of Law"), P("openai", 2, "School of Law"), P("gemini", 1, "Law School"),
             P("anthropic", 1, "Office of Career Services")]
    answered = [(p, s) for p in ("openai", "anthropic", "gemini") for s in (1, 2, 3)]
    groups = analyze(props, answered)
    law = next(g for g in groups if g["name"] == "School of Law")
    assert (law["n_providers"], law["n_runs"], law["score"]) == (2, 3, round(3 / 9, 4))
    assert law["providers"] == ["openai", "gemini"] and law["status"] == "correct"
    office = next(g for g in groups if g["name"] == "Office of Career Services")
    assert office["status"] == "wrong" and office["wrong_kind"] == "non_academic" and office["n_providers"] == 1


def test_wrong_kinds():
    assert ag.wrong_kind(["University Libraries"], ["school"]) == "non_academic"
    assert ag.wrong_kind(["Undergraduate Admissions"], [""]) == "non_academic"
    assert ag.wrong_kind(["Brooklyn Campus"], ["campus"]) == "campus"
    assert ag.wrong_kind(["Anything"], ["administrative unit"]) == "non_academic"
    assert ag.wrong_kind(["School of Underwater Basketweaving"], ["school"]) == "invented"


def test_a_second_group_for_one_true_school_is_a_duplicate():
    """Two groups that both match one true school: one is correct, the other is a
    duplicate and counts as wrong (a repeated name counts once, as in week 2)."""
    matched, dups = ag.match_groups_to_truth([["Stern School of Business"], ["Stern School of Business (Undergraduate)"]],
                                             TRUTH, match=lambda a, b: "Stern" in a and "Stern" in b)
    assert matched == {0: 1} and dups == {1: 1}


def test_matching_is_one_to_one_and_maximum():
    # group 0 fits schools 0 and 1, group 1 fits only school 0: both must match
    edges = {("g0", "t0"), ("g0", "t1"), ("g1", "t0")}
    matched, dups = ag.match_groups_to_truth([["g0"], ["g1"]], ["t0", "t1"], match=lambda a, b: (a, b) in edges)
    assert matched == {0: 1, 1: 0} and dups == {}


def test_precision_by_agreement_level():
    groups = [{"university_set": "eval", "n_providers": k, "n_runs": r, "status": s}
              for k, r, s in [(3, 9, "correct"), (3, 7, "correct"), (2, 4, "correct"), (2, 2, "wrong"),
                              (1, 1, "wrong"), (1, 3, "correct")]]
    rows = ag.precision_by_agreement(groups, 3, 3)
    get = lambda level, k: next(r for r in rows if r["university_set"] == "all" and r["level"] == level and r["k"] == k)
    assert get("providers_exactly", 3)["precision"] == 1.0
    assert get("providers_exactly", 2)["precision"] == 0.5
    assert get("providers_exactly", 1)["precision"] == 0.5
    assert get("providers_at_least", 2)["precision"] == 0.75
    assert get("runs_exactly", 5)["precision"] == ""                     # no school at that level
    assert get("runs_exactly", 9)["schools"] == 1


def test_cross_provider_error_rates():
    groups = [{"providers": ["openai", "gemini"], "status": "wrong"},
              {"providers": ["openai"], "status": "wrong"},
              {"providers": ["openai", "gemini"], "status": "correct"},
              {"providers": ["gemini"], "status": "correct"}]
    rows = {(r["provider_a"], r["provider_b"]): r for r in ag.cross_provider(groups, ["openai", "gemini"])}
    assert rows[("openai", "gemini")]["rate_wrong"] == 0.5             # 1 of openai's 2 wrong ones
    assert rows[("gemini", "openai")]["rate_wrong"] == 1.0
    assert rows[("openai", "gemini")]["rate_correct"] == 1.0


def test_wrong_by_provider_splits_the_kinds():
    groups = [{"university_set": "eval", "providers": ["openai"], "n_providers": 1, "status": "wrong", "wrong_kind": "invented"},
              {"university_set": "eval", "providers": ["openai", "gemini"], "n_providers": 2, "status": "wrong", "wrong_kind": "non_academic"},
              {"university_set": "eval", "providers": ["openai"], "n_providers": 1, "status": "correct", "wrong_kind": ""}]
    rows = {(r["university_set"], r["provider"]): r for r in ag.wrong_by_provider(groups, ["openai", "gemini"])}
    o = rows[("all", "openai")]
    assert (o["schools"], o["wrong"], o["invented"], o["non_academic"], o["only_this_provider_wrong"]) == (3, 2, 1, 1, 1)
    assert rows[("all", "gemini")]["invented"] == 0


# ── capture and recapture ────────────────────────────────────────────────

def test_chapman():
    assert ag.chapman(10, 10, 10) == pytest.approx(10.0)
    assert ag.chapman(10, 8, 4) == pytest.approx(11 * 9 / 5 - 1)
    assert ag.chapman(3, 3, 0) == pytest.approx(15.0)                  # defined with no overlap


def test_mt_with_two_lists_is_lincoln_petersen():
    a, b = set(range(10)), set(range(5, 13))                             # 10 and 8, 5 on both
    assert ag.mt_estimate([a, b]) == pytest.approx(10 * 8 / 5, rel=1e-6)


def test_mt_edge_cases():
    assert ag.mt_estimate([set(), set()]) == 0.0
    assert ag.mt_estimate([{1, 2}, {3}]) is None                         # no overlap: no upper bound
    assert ag.mt_estimate([{1, 2, 3}] * 3) == pytest.approx(3.0)          # every list saw everything


def test_mt_three_lists_is_at_least_what_was_seen():
    sets = [{1, 2, 3, 4}, {2, 3, 4, 5}, {1, 3, 5, 6}]
    est = ag.mt_estimate(sets)
    assert est >= 6
    n = est                                                               # it solves the M_t equation
    lhs = (1 - 4 / n) ** 3
    assert lhs == pytest.approx(1 - 6 / n, abs=1e-9)


def test_s1_counts_two_ways():
    """`all` counts every group; `matched` only those that match a true school."""
    props = [P("openai", 1, "School of Law"), P("gemini", 1, "School of Law"),
             P("openai", 1, "Stern School of Business"), P("gemini", 1, "Made Up Institute of Nothing")]
    answered = [("openai", 1), ("gemini", 1)]
    groups = ag.analyze_university("Q1", props, TRUTH, answered)
    rows = ag.s1_estimates("Q1", "U", "eval", 3, groups, props, ["openai", "gemini"], answered)
    pair = {r["mode"]: r for r in rows if r["method"] == "chapman_pair"}
    assert pair["all"]["observed"] == 3 and pair["matched"]["observed"] == 2
    assert pair["all"]["estimate"] == round(ag.chapman(2, 2, 1), 2)
    assert pair["matched"]["estimate"] == round(ag.chapman(2, 1, 1), 2)
    assert pair["all"]["true_count"] == 3
    # one sample per provider: no repeated-run estimate
    assert not [r for r in rows if r["method"] == "mt_samples"]


def test_s1_repeated_samples_of_one_provider():
    props = [P("openai", 1, "School of Law"), P("openai", 2, "School of Law"), P("openai", 2, "Stern School of Business")]
    answered = [("openai", 1), ("openai", 2)]
    groups = ag.analyze_university("Q1", props, TRUTH, answered)
    rows = ag.s1_estimates("Q1", "U", "eval", 3, groups, props, ["openai"], answered)
    mt = [r for r in rows if r["method"] == "mt_samples" and r["mode"] == "all"]
    assert len(mt) == 1 and mt[0]["sources"] == "openai#1+openai#2" and mt[0]["estimate"] == 2.0


# ── review sample and the small-university draw ──────────────────────────

def test_review_sample_is_one_provider_schools_and_repeatable():
    groups = [{"group_id": f"g{i:02d}", "n_providers": 1 if i % 2 else 2} for i in range(60)]
    a = ag.review_sample(groups, 20, 7)
    assert len(a) == 20 and all(g["n_providers"] == 1 for g in a)
    assert a == ag.review_sample(list(reversed(groups)), 20, 7)


def test_draw_small_uses_the_bottom_third_and_the_seed():
    unis = [(f"Q{i}", f"U{i}", i % 10) for i in range(1, 31)]          # 30 universities
    drawn = ag.draw_small(unis, seed=1)
    assert len(drawn) == 6 and len({q for q, _, _ in drawn}) == 6
    bottom = sorted(unis, key=lambda u: (u[2], int(u[0][1:])))[:10]
    assert set(drawn) <= set(bottom)
    assert drawn == ag.draw_small(list(reversed(unis)), seed=1)        # input order does not matter


def test_draw_small_reproduces_the_week_3_draw():
    """Seed 2026 on the saved sitelink snapshot gives the 6 universities in the ground truth."""
    drawn = ag.draw_small(ag.sitelinks_table(), seed=2026)
    assert [q for q, _, _ in drawn] == ["Q6841624", "Q4929506", "Q7114753", "Q7896431", "Q7208107", "Q38251806"]


# ── the whole report, without any call ───────────────────────────────────

def test_analyze_all_and_files_are_repeatable(tmp_path):
    universities = [{"qid": "Q1", "set": "eval", "name": "U", "website": "", "schools": TRUTH},
                    {"qid": "Q2", "set": "small", "name": "Small", "website": "", "schools": []}]

    def answers():
        props = {"Q1": [P("openai", 1, "School of Law", 0), P("gemini", 1, "Law School", 0),
                        P("openai", 1, "Undergraduate Admissions", 1), P("gemini", 2, "Tisch School of the Arts", 0)],
                 "Q2": [P("openai", 1, "School of Nursing", 0)]}
        runs = {q: [{"provider": p, "sample": s, "answered": any(x["provider"] == p and x["sample"] == s for x in props[q])}
                    for p in ("openai", "gemini") for s in (1, 2)] for q in props}
        return props, runs

    outs = []
    for name in ("a", "b"):
        props, runs = answers()
        tables = ag.analyze_all(universities, props, runs, ["openai", "gemini"], 2)
        ag.write_report(tmp_path / name, tables, [r for q in runs for r in runs[q]])
        outs.append(tmp_path / name)
    assert ag.compare_dirs(*outs) == []
    groups = (outs[0] / "groups.csv").read_text()
    assert "Q2-g01" in groups and "invented" in groups                  # a school at a college with none
    assert (outs[0] / "single_provider_review.csv").exists()


# ── Gemini: thinking off for sampled calls ───────────────────────────────

def fake_gemini(monkeypatch, tmp_path):
    import wikidata_discover.config as config
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "k")
    seen = []

    class Models:
        @staticmethod
        def generate_content(model, contents, config):
            seen.append(config)
            text = json.dumps({"units": [{"name": "School of Law", "unit_type": "school", "city": "",
                                          "state": "", "website": None}], "reference": "https://x.edu"})
            return type("R", (), {"text": text})()
    monkeypatch.setattr(lh, "_get_gemini_client", lambda: type("C", (), {"models": Models})())
    return seen


def test_a_sampled_gemini_call_turns_thinking_off(monkeypatch, tmp_path):
    """With thinking on, Gemini spent its output budget thinking and cut most answers
    off (finish reason MAX_TOKENS, 9 of 36 answers in the first week 3 run)."""
    seen = fake_gemini(monkeypatch, tmp_path)
    LLMHelper.extract_divisions_gemini("NYU", "https://www.nyu.edu", sample=1)
    assert seen[0].thinking_config.thinking_budget == 0
    LLMHelper.extract_divisions_gemini("NYU", "https://www.nyu.edu")              # no sample: unchanged
    assert seen[1].thinking_config is None


def test_the_gemini_sample_key_names_thinking_off():
    """A sampled Gemini key differs from the one the first week 3 run used (thinking
    on), so the new run never reads those answers; the old files stay where they are."""
    with_thinking = lh._cache_key("NYU", "gemini", "m", extra=lh._extract_extra("https://www.nyu.edu", 1))
    assert lh._cache_key("NYU", "gemini", "m", extra=lh._extract_key_extra("gemini", "https://www.nyu.edu", 1)) != with_thinking
    # other providers and unsampled Gemini calls keep their keys
    assert lh._extract_key_extra("openai", "w", 1) == lh._extract_extra("w", 1)
    assert lh._extract_key_extra("gemini", "w", None) == "w"


def test_extract_cache_key_is_the_key_each_extractor_uses(monkeypatch, tmp_path):
    fake_gemini(monkeypatch, tmp_path)
    LLMHelper.extract_divisions_gemini("NYU", "https://www.nyu.edu", sample=2)
    assert [p.stem for p in tmp_path.glob("*.json") if not p.name.endswith(".meta.json")] == \
        [lh.extract_cache_key("gemini", "NYU", "https://www.nyu.edu", 2)]
