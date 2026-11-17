"""
The LLM judge, tested (Shuo, week 4; see TASKS.md and SPECS.md).

The merged school list of each university comes from week 3: every group of name
variants that any provider named (`agreement.py`, read from the cache, no new
extraction calls). Each provider then takes a turn as judge of that list. Each judge
runs four times: twice with the list in order A (names sorted A to Z) and twice in
order B (the reverse). Each run is its own call with its own cache entry.

The report answers three questions:
1. How often does a verdict flip between two runs in the same order, and between
   two runs in different orders? The order effect is the difference.
2. Does a judge keep its own provider's schools more often than the schools of the
   other providers, at the same correctness?
3. Does the judge gain in precision (it removes wrong schools) or in recall (it
   keeps right ones)?

Usage:
    python -m wikidata_discover.eval.judge                         # 18 universities, 3 judges
    python -m wikidata_discover.eval.judge --no-run-log            # rescore from the cache only
    python -m wikidata_discover.eval.judge --hand-list FILE.csv    # check 1: fake schools
    python -m wikidata_discover.eval.judge --compare DIR_A DIR_B

Output (wikidata_discover/results/judge/<run_id>/):
    merged_lists.csv         the list each judge saw: one row per school and order position
    verdicts.csv             one row per school, judge, and run: kept (1), dropped (0), or blank
    judge_runs.csv           one row per judge call: answered or not, unmapped names
    flip_rates.csv           same-order and cross-order flip rates, and the order effect
    self_preference.csv      keep rate of a judge's own schools against the others' schools
    precision_recall.csv     precision and recall before and after each judge, with baselines
"""

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from wikidata_discover.eval import agreement
from wikidata_discover.eval.agreement import PROVIDERS, provider_order, university_matcher, write_csv

ORDERS = ("A", "B")
SAMPLES_PER_ORDER = 2
RUNS: Tuple[Tuple[str, int], ...] = tuple((o, s) for o in ORDERS for s in range(1, SAMPLES_PER_ORDER + 1))
WEEK3_RUN = "agreement-20261109T204358Z-32c21a"
SETS = ("eval", "small", "all")


# ─────────────────────────  THE LIST AND ITS ORDERS  ─────────────────────────

def ordered(names: Sequence[str], order: str) -> List[str]:
    """Order A: the names sorted A to Z, ignoring case (ties by exact text). Order B:
    order A reversed, so every name moves as far as it can on average."""
    a = sorted(names, key=lambda n: (n.casefold(), n))
    if order == "A":
        return a
    if order == "B":
        return a[::-1]
    raise ValueError(f"order must be A or B, not {order!r}")


def run_name(judge: str, order: str, sample: int) -> str:
    return f"{judge}:{order}{sample}"


def _plain(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


def map_kept(kept: Sequence[str], names: Sequence[str],
             match: Callable[[str, str], bool]) -> Tuple[Dict[int, str], List[str]]:
    """Map the names a judge returned to the list it saw. Each returned name takes the
    first list entry not yet taken that equals it exactly, else after case and
    punctuation are ignored, else that matches it as a name variant. Returns
    ({list index: returned name}, [returned names that fit no entry])."""
    taken: Dict[int, str] = {}
    unmapped: List[str] = []
    for k in kept:
        for test in (lambda n: n == k, lambda n: _plain(n) == _plain(k), lambda n: match(n, k)):
            hit = next((i for i, n in enumerate(names) if i not in taken and test(n)), None)
            if hit is not None:
                taken[hit] = k
                break
        else:
            unmapped.append(k)
    return taken, unmapped


# ─────────────────────────  RUNNING THE JUDGES  ─────────────────────────

def judge_university(university: dict, groups: List[dict], judges: Sequence[str],
                     ask: Callable[..., Optional[List[str]]], log=None,
                     week3_checks: Optional[Dict[str, dict]] = None, level: str = "school",
                     match: Optional[Callable[[str, str], bool]] = None) -> Tuple[List[dict], List[dict], List[dict]]:
    """Every judge run on one university's merged list. `ask(label, names, judge,
    sample, order)` returns the kept names or None (no answer). Returns (list rows,
    verdict rows, run rows). With a run log, each verdict is also a `checks` row
    (kind judge) on the candidate of the week 3 agreement check of that school."""
    from wikidata_discover import llm_helpers
    label, qid = university["name"], university["qid"]
    match = match or university_matcher(label)
    by_name = {g["name"]: g for g in groups}
    list_rows, verdicts, runs = [], [], []
    for order in ORDERS:
        for pos, n in enumerate(ordered(list(by_name), order), 1):
            list_rows.append({"university_qid": qid, "order": order, "position": pos, "name": n,
                              "group_id": by_name[n]["group_id"]})
    for judge in judges:
        for order, sample in RUNS:
            names = ordered(list(by_name), order)
            kept = ask(label, names, judge, sample, order)
            call_id = llm_helpers.last_judge_call_id
            answered = kept is not None
            taken, unmapped = map_kept(kept or [], names, match)
            runs.append({"university_qid": qid, "judge": judge, "order": order, "sample": sample,
                         "listed": len(names), "answered": answered, "kept": len(taken) if answered else "",
                         "unmapped": unmapped, "cache_key": llm_helpers.judge_cache_key(judge, label, names, sample, level, order)})
            for pos, n in enumerate(names, 1):
                g = by_name[n]
                verdict = (1 if (pos - 1) in taken else 0) if answered else ""
                verdicts.append({"university_qid": qid, "university_set": university["set"],
                                 "group_id": g["group_id"], "name": n, "status": g["status"],
                                 "providers": g["providers"], "n_providers": g["n_providers"],
                                 "judge": judge, "order": order, "sample": sample, "position": pos,
                                 "kept": verdict, "returned_name": taken.get(pos - 1, "")})
                w3 = (week3_checks or {}).get(g["group_id"])
                if log is not None and w3 and w3.get("candidate_id"):
                    log.add_check(kind="judge", result={1: "pass", 0: "fail", "": "unclear"}[verdict],
                                  candidate_id=w3["candidate_id"], score=verdict if answered else None,
                                  llm_call_ids=[call_id] if call_id else [],
                                  detail={"university_qid": qid, "group_id": g["group_id"], "name": n,
                                          "judge": judge, "order": order, "sample": sample,
                                          "position": pos, "list_length": len(names),
                                          "returned_name": taken.get(pos - 1, ""),
                                          "agreement_check_id": w3.get("check_id"),
                                          "agreement_run_id": WEEK3_RUN})
    return list_rows, verdicts, runs


# ─────────────────────────  ANALYSIS  ─────────────────────────

def _rate(num: int, den: int):
    return round(num / den, 4) if den else ""


def _sets_of(rows: List[dict]) -> List[str]:
    present = {r["university_set"] for r in rows}
    return [s for s in SETS[:-1] if s in present] + sorted(present - set(SETS)) + ["all"]


def flip_rates(verdicts: List[dict], judges: Sequence[str]) -> List[dict]:
    """For each judge and university set: the share of pairs of runs that give one
    school different verdicts. Same order: (A1, A2) and (B1, B2). Cross order: every
    pair of an A run and a B run. A pair counts only when both runs answered. The
    order effect is cross minus same: what the order adds to ordinary randomness."""
    by_item: Dict[Tuple[str, str, str], Dict[Tuple[str, int], int]] = {}
    sets_of_item: Dict[Tuple[str, str, str], str] = {}
    for v in verdicts:
        if v["kept"] == "":
            continue
        k = (v["judge"], v["university_qid"], v["group_id"])
        by_item.setdefault(k, {})[(v["order"], v["sample"])] = v["kept"]
        sets_of_item[k] = v["university_set"]
    same_pairs = [(("A", 1), ("A", 2)), (("B", 1), ("B", 2))]
    cross_pairs = [(a, b) for a in RUNS if a[0] == "A" for b in RUNS if b[0] == "B"]
    rows = []
    for judge in list(judges) + ["all"]:
        for uset in _sets_of(verdicts):
            counts = {"same": [0, 0], "cross": [0, 0]}
            for k, runs in by_item.items():
                if (judge != "all" and k[0] != judge) or (uset != "all" and sets_of_item[k] != uset):
                    continue
                for kind, pairs in (("same", same_pairs), ("cross", cross_pairs)):
                    for a, b in pairs:
                        if a in runs and b in runs:
                            counts[kind][0] += 1
                            counts[kind][1] += runs[a] != runs[b]
            same, cross = _rate(counts["same"][1], counts["same"][0]), _rate(counts["cross"][1], counts["cross"][0])
            rows.append({"judge": judge, "university_set": uset,
                         "same_order_pairs": counts["same"][0], "same_order_flips": counts["same"][1],
                         "same_order_flip_rate": same,
                         "cross_order_pairs": counts["cross"][0], "cross_order_flips": counts["cross"][1],
                         "cross_order_flip_rate": cross,
                         "order_effect": round(cross - same, 4) if same != "" and cross != "" else ""})
    return rows


def self_preference(verdicts: List[dict], judges: Sequence[str]) -> List[dict]:
    """For each judge and correctness (correct, wrong): the keep rate of schools its own
    provider named against schools only other providers named, over every answered run.
    Scope `all` uses every school. Scope `one_provider` uses only schools that exactly
    one provider named, so the comparison is not mixed up with agreement (schools that
    3 providers named are both more often right and always named by the judge)."""
    rows = []
    for judge in judges:
        for scope in ("all", "one_provider"):
            for status in ("correct", "wrong"):
                sel = [v for v in verdicts if v["judge"] == judge and v["kept"] != "" and v["status"] == status
                       and (scope == "all" or v["n_providers"] == 1)]
                own = [v for v in sel if judge in v["providers"]]
                other = [v for v in sel if judge not in v["providers"]]
                ok, ot = sum(v["kept"] for v in own), sum(v["kept"] for v in other)
                ro, rt = _rate(ok, len(own)), _rate(ot, len(other))
                rows.append({"judge": judge, "scope": scope, "status": status,
                             "own_verdicts": len(own), "own_kept": ok, "own_keep_rate": ro,
                             "other_verdicts": len(other), "other_kept": ot, "other_keep_rate": rt,
                             "difference": round(ro - rt, 4) if ro != "" and rt != "" else ""})
    return rows


def matched_count(kept: List[dict]) -> int:
    """How many true schools the kept groups cover: a maximum one-to-one matching of
    the kept groups to true schools, per university. It is computed again for each
    kept subset, so a judge that drops the correct group and keeps its duplicate
    still covers the school once. A group's options are `truth_options`; without
    them, a correct group counts as its own school. Pure."""
    total = 0
    by_u: Dict[str, List[dict]] = {}
    for g in kept:
        by_u.setdefault(g["university_qid"], []).append(g)
    for gs in by_u.values():
        edges = []
        for g in gs:
            opts = g.get("truth_options")
            if opts is None:
                opts = [g.get("truth_name") or g["group_id"]] if g["status"] == "correct" else []
            edges.append(list(opts))
        owner: Dict[str, int] = {}

        def augment(i: int, seen: set) -> bool:
            for t in edges[i]:
                if t not in seen:
                    seen.add(t)
                    if t not in owner or augment(owner[t], seen):
                        owner[t] = i
                        return True
            return False
        total += sum(1 for i in range(len(edges)) if augment(i, set()))
    return total


def _pr_row(method: str, run: str, uset: str, kept: List[dict], total_correct: int, total_wrong: int,
            true_count: int) -> dict:
    c = matched_count(kept)
    w = len(kept) - c
    return {"method": method, "run": run, "university_set": uset, "schools_kept": len(kept),
            "correct_kept": c, "wrong_kept": w, "precision": _rate(c, len(kept)),
            "recall": _rate(c, true_count), "share_of_correct_kept": _rate(c, total_correct),
            "share_of_wrong_removed": _rate(total_wrong - w, total_wrong), "universities_not_answered": ""}


def precision_recall(groups: List[dict], verdicts: List[dict], judges: Sequence[str],
                     true_counts: Dict[str, int], university_sets: Optional[Dict[str, str]] = None) -> List[dict]:
    """Precision and recall of the merged list before any filter, after agreement
    filters (2 or more providers, all 3 providers), and after each judge: per run,
    the mean over its runs, and the majority (kept in 3 or more of 4 runs). Recall
    counts true schools matched by a kept school, over all true schools of the set.
    The kept schools are matched to the true schools again for each filter and run
    (matched_count), so a kept duplicate counts as correct when its school's first
    group was dropped.
    A run that did not answer for a university counts as keeping nothing there.
    university_sets (qid -> set) puts every requested university in its set, so a
    university with no groups still adds its true count to that set's recall."""
    rows = []
    sets = university_sets or {}
    for uset in _sets_of(groups + [{"university_set": s} for s in sets.values()]):
        gs = [g for g in groups if uset == "all" or g["university_set"] == uset]
        members = {g["university_qid"] for g in gs} | {q for q, s in sets.items() if s == uset}
        tc = sum(true_counts[q] for q in members) if uset != "all" else sum(true_counts.values())
        correct = sum(1 for g in gs if g["status"] == "correct")
        wrong = len(gs) - correct
        rows.append(_pr_row("no_filter", "", uset, gs, correct, wrong, tc))
        rows.append(_pr_row("agreement_2plus", "", uset, [g for g in gs if g["n_providers"] >= 2], correct, wrong, tc))
        rows.append(_pr_row("agreement_3", "", uset, [g for g in gs if g["n_providers"] >= 3], correct, wrong, tc))
        ids = {g["group_id"] for g in gs}
        gmap = {g["group_id"]: g for g in gs}
        for judge in judges:
            vs = [v for v in verdicts if v["judge"] == judge and v["group_id"] in ids]
            per_run = []
            for order, sample in RUNS:
                kept = [gmap[v["group_id"]] for v in vs if v["order"] == order and v["sample"] == sample and v["kept"] == 1]
                r = _pr_row(f"judge_{judge}", f"{order}{sample}", uset, kept, correct, wrong, tc)
                r["universities_not_answered"] = len({v["university_qid"] for v in vs if v["order"] == order
                                                      and v["sample"] == sample and v["kept"] == ""})
                rows.append(r)
                per_run.append(r)
            mean = {"method": f"judge_{judge}", "run": "mean", "university_set": uset}
            for col in ("schools_kept", "correct_kept", "wrong_kept", "precision", "recall",
                        "share_of_correct_kept", "share_of_wrong_removed"):
                vals = [r[col] for r in per_run if r[col] != ""]
                mean[col] = round(sum(vals) / len(vals), 4) if vals else ""
            rows.append(mean)
            votes: Dict[str, int] = {}
            for v in vs:
                if v["kept"] == 1:
                    votes[v["group_id"]] = votes.get(v["group_id"], 0) + 1
            majority = [gmap[i] for i in sorted(votes) if votes[i] >= 3]
            rows.append(_pr_row(f"judge_{judge}", "majority_3_of_4", uset, majority, correct, wrong, tc))
    return rows


def analyze(groups: List[dict], verdicts: List[dict], judges: Sequence[str],
            true_counts: Dict[str, int], university_sets: Optional[Dict[str, str]] = None) -> Dict[str, List[dict]]:
    """Every table of the report. Pure: no calls, no files."""
    return {"flip_rates": flip_rates(verdicts, judges),
            "self_preference": self_preference(verdicts, judges),
            "precision_recall": precision_recall(groups, verdicts, judges, true_counts, university_sets)}


# ─────────────────────────  CHECK 1: A HAND-MADE LIST WITH FAKE SCHOOLS  ─────────────────────────

HAND_KINDS = {"yes": "yes", "y": "yes", "true": "yes", "1": "yes", "wrong": "wrong", "known_wrong": "wrong"}


def read_hand_list(path: Path) -> Dict[str, List[Tuple[str, str]]]:
    """A CSV with columns university_qid, name, fake. fake is yes (a planted fake), wrong
    (a known wrong school, such as a wrong week 3 group), or no or empty (a real school).
    Returns {qid: [(name, kind), ...]} in file order, kind being yes, wrong, or no."""
    out: Dict[str, List[Tuple[str, str]]] = {}
    with Path(path).open() as f:
        for r in csv.DictReader(f):
            name = (r.get("name") or "").strip()
            if not name:
                continue
            kind = HAND_KINDS.get((r.get("fake") or "").strip().lower(), "no")
            out.setdefault(r["university_qid"].strip(), []).append((name, kind))
    return out


def judge_hand_list(hand: Dict[str, List[Tuple[str, str]]], universities: Dict[str, dict],
                    judges: Sequence[str], ask: Callable[..., Optional[List[str]]]) -> List[dict]:
    """Each judge, 4 runs, on each hand-made list. One row per school, judge, and run:
    kept or not, and the school's kind. A fake or known wrong school must be dropped."""
    rows = []
    for qid, items in hand.items():
        label = universities[qid]["name"]
        match = university_matcher(label)
        kind = {n: k for n, k in items}
        for judge in judges:
            for order, sample in RUNS:
                names = ordered(list(kind), order)
                kept = ask(label, names, judge, sample, order)
                taken, _ = map_kept(kept or [], names, match)
                for pos, n in enumerate(names, 1):
                    k = (1 if (pos - 1) in taken else 0) if kept is not None else ""
                    rows.append({"university_qid": qid, "name": n, "fake": kind[n],
                                 "judge": judge, "order": order, "sample": sample, "kept": k,
                                 "correct_verdict": "" if k == "" else int(k == (1 if kind[n] == "no" else 0))})
    return rows


def hand_summary(rows: List[dict]) -> List[dict]:
    """Per judge: fake schools removed, known wrong schools removed, and real schools
    kept, over all answered runs. removed_every_fake_every_run needs every scheduled
    verdict on a fake to drop it, so an unanswered run makes it False."""
    out = []
    for judge in sorted({r["judge"] for r in rows}, key=provider_order):
        mine = [r for r in rows if r["judge"] == judge and r["kept"] != ""]
        fakes = [r for r in mine if r["fake"] == "yes"]
        all_fakes = [r for r in rows if r["judge"] == judge and r["fake"] == "yes"]
        wrongs = [r for r in mine if r["fake"] == "wrong"]
        reals = [r for r in mine if r["fake"] == "no"]
        out.append({"judge": judge, "fake_verdicts": len(fakes),
                    "fake_removed": sum(1 for r in fakes if r["kept"] == 0),
                    "known_wrong_verdicts": len(wrongs),
                    "known_wrong_removed": sum(1 for r in wrongs if r["kept"] == 0),
                    "real_verdicts": len(reals), "real_kept": sum(1 for r in reals if r["kept"] == 1),
                    "fakes_kept_at_least_once": sorted({r["name"] for r in fakes if r["kept"] == 1}),
                    "known_wrong_kept_at_least_once": sorted({r["name"] for r in wrongs if r["kept"] == 1}),
                    "removed_every_fake_every_run": bool(all_fakes) and all(r["kept"] == 0 for r in all_fakes)})
    return out


# ─────────────────────────  DEPARTMENTS  ─────────────────────────

DEPT_SCHOOLS = Path(__file__).parent / "dept_schools.csv"
DEPT_TRUTH = Path(__file__).parent / "dept_ground_truth.csv"
DEPT_SAMPLES = 3


def load_dept_schools(path: Path = DEPT_SCHOOLS) -> List[dict]:
    """The schools of the department ground truth: parent (as in the ground truth),
    school_qid, school_label, university. `set` is nyu or other."""
    with Path(path).open() as f:
        rows = [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    for r in rows:
        r["set"] = "nyu" if r["university"] == "New York University" else "other"
        r["id"] = school_stem(r)      # the QID, or noitem-<name> for a school with no item
    return rows


def school_stem(school: dict) -> str:
    """The file name part of a school: its QID, or for a school with no item ("none")
    the name that `subunits --no-item` gives its files."""
    from wikidata_discover.subunits import unit_stem
    qid = school.get("school_qid") or ""
    return unit_stem({"qid": qid if qid.lower() not in ("", "none") else None, "label": school.get("school_label")})


def dept_list_path(folder: Path, stem: str, provider: str, sample: int) -> Path:
    """The department CSV that `subunits <QID> --provider P --sample N` writes."""
    return Path(folder) / f"{stem}_{provider}_s{sample}_departments.csv"


def read_dept_lists(folder: Path, school: dict, providers: Sequence[str],
                    samples: int) -> Tuple[List[dict], List[dict]]:
    """Every department list of one school: (proposals, run rows). A list file that does
    not exist is a run that did not answer; a file with only a header is an empty answer."""
    proposals, runs = [], []
    for p in providers:
        for s in range(1, samples + 1):
            path = dept_list_path(folder, school_stem(school), p, s)
            names = []
            if path.exists():
                with path.open() as f:
                    names = [(r.get("name") or "").strip() for r in csv.DictReader(f)]
            names = [n for n in names if n]
            for i, n in enumerate(names):
                proposals.append({"university_qid": school["id"], "provider": p, "sample": s,
                                  "index": i, "name": n, "unit_type": "department"})
            runs.append({"school_qid": school["id"], "provider": p, "sample": s, "file": str(path),
                         "file_exists": path.exists(), "departments": len(names), "answered": path.exists()})
    return proposals, runs


def score_dept_lists(truth, school: dict, proposals: List[dict], runs: List[dict]) -> List[dict]:
    """Each provider's list of each sample, scored with the week 2 department scorer
    against the school's part of the ground truth."""
    from wikidata_discover.eval import dept_scorer as ds
    out = []
    for r in runs:
        names = [p["name"] for p in proposals if p["provider"] == r["provider"] and p["sample"] == r["sample"]]
        m = ds.score(truth, ds.ProposedList([ds.ProposedDept(n) for n in names], False, False),
                     parent=school["parent"]).metrics if r["answered"] else {}
        out.append({"school_qid": school["id"], "parent": school["parent"], "set": school["set"],
                    "provider": r["provider"], "sample": r["sample"], "answered": r["answered"],
                    "departments": r["departments"],
                    **{k: ("" if m.get(k) is None else round(m[k], 4))
                       for k in ("name_precision", "name_recall", "name_f1")}})
    return out


def dept_groups(truth, school: dict, proposals: List[dict], runs: List[dict]) -> List[dict]:
    """The merged department list of one school: name variants grouped across all
    lists, each group matched one to one to the ground truth (correct or wrong)."""
    from wikidata_discover.eval import dept_scorer as ds
    true_names = [t.name for t in truth.rows if t.parent == school["parent"]]
    answered = [(r["provider"], r["sample"]) for r in runs if r["answered"]]
    groups = agreement.analyze_university(school["id"], proposals, true_names, answered,
                                          match=ds.dept_names_match)
    for g in groups:
        g["university_set"] = school["set"]
    return groups


def run_departments(args, judges: Sequence[str], out_dir: Path, log) -> Dict[str, List[dict]]:
    """Score every department list, merge the lists of each school, and judge the
    merged list. Writes the tables under out_dir/departments/."""
    from wikidata_discover import config
    from wikidata_discover.batch import RESULTS_DIR
    from wikidata_discover.eval import dept_scorer as ds
    console = config.console
    truth = ds.load_ground_truth(Path(args.dept_truth or DEPT_TRUTH))
    schools = load_dept_schools(Path(args.dept_schools or DEPT_SCHOOLS))
    known = {t.parent for t in truth.rows}
    missing = [s["parent"] for s in schools if s["parent"] not in known]
    if missing:
        raise SystemExit(f"Schools with no rows in the department ground truth: {missing}")
    folder = Path(args.subunits_dir) if args.subunits_dir else RESULTS_DIR / "subunits"
    ask = cached_ask(args.no_run_log, level="department")
    scores, groups, lists, verdicts, runs, files = [], [], [], [], [], []
    for school in schools:
        if log is not None:
            log.set_university(school.get("university_qid") or None)
        props, list_runs = read_dept_lists(folder, school, PROVIDERS, args.dept_samples)
        files += list_runs
        scores += score_dept_lists(truth, school, props, list_runs)
        gs = dept_groups(truth, school, props, list_runs)
        groups += gs
        label = f"{school['school_label']}, {school['university']}"
        l, v, r = judge_university({"qid": school["id"], "name": label, "set": school["set"]}, gs,
                                   judges, ask, log, level="department", match=ds.dept_names_match)
        lists, verdicts, runs = lists + l, verdicts + v, runs + r
        if log is not None:
            log.flush()
        absent = [agreement.run_label(x["provider"], x["sample"]) for x in list_runs if not x["file_exists"]]
        console.print(f"  {school['id']} {school['school_label']}: {len(gs)} departments merged"
                      + (f"; no list from {absent}" if absent else ""))
    true_counts = {s["id"]: sum(1 for t in truth.rows if t.parent == s["parent"]) for s in schools}
    tables = analyze(groups, verdicts, judges, true_counts, {s["id"]: s["set"] for s in schools})
    d = out_dir / "departments"
    d.mkdir(parents=True, exist_ok=True)
    write_csv(d / "list_files.csv", files)
    write_csv(d / "list_scores.csv", scores)
    write_csv(d / "groups.csv", groups, agreement.GROUP_COLUMNS)
    write_csv(d / "merged_lists.csv", lists)
    write_csv(d / "verdicts.csv", verdicts, VERDICT_COLUMNS)
    write_csv(d / "judge_runs.csv", runs, RUN_COLUMNS)
    for name in ("flip_rates", "self_preference", "precision_recall"):
        write_csv(d / f"{name}.csv", tables[name])
    return {**tables, "list_scores": scores}


# ─────────────────────────  FILES AND COMMAND  ─────────────────────────

VERDICT_COLUMNS = ["university_qid", "university_set", "group_id", "name", "status", "providers",
                   "n_providers", "judge", "order", "sample", "position", "kept", "returned_name"]
RUN_COLUMNS = ["university_qid", "judge", "order", "sample", "listed", "answered", "kept", "unmapped", "cache_key"]
COMPARED_FILES = ("merged_lists.csv", "verdicts.csv", "flip_rates.csv", "self_preference.csv",
                  "precision_recall.csv")


DEPT_COMPARED_FILES = ("list_scores.csv", "groups.csv", "merged_lists.csv", "verdicts.csv", "flip_rates.csv",
                       "self_preference.csv", "precision_recall.csv")


def compare_dirs(a: Path, b: Path) -> List[str]:
    """Names of the report files that differ between two output folders. The files of
    a department run are under departments/. A file missing on one side differs."""
    def same(f):
        fa, fb = a / f, b / f
        return fa.exists() == fb.exists() and (not fa.exists() or fa.read_bytes() == fb.read_bytes())
    files = list(COMPARED_FILES) if (a / COMPARED_FILES[0]).exists() or (b / COMPARED_FILES[0]).exists() else []
    if (a / "departments").exists() or (b / "departments").exists():
        files += [f"departments/{f}" for f in DEPT_COMPARED_FILES]
    return [f for f in files if not same(f)]


def week3_agreement_checks(run_id: str = WEEK3_RUN) -> Dict[str, dict]:
    """{group_id: {check_id, candidate_id}} of the agreement checks of a week 3 run, so
    a judge check names the same candidate. Group ids are stable: the same cached
    answers always give the same groups."""
    import json
    from google.cloud import bigquery
    from wikidata_discover import config
    client = bigquery.Client(project=config.GCP_PROJECT)
    q = (f"SELECT check_id, candidate_id, detail FROM `{config.GCP_PROJECT}.{config.RUNLOG_DATASET}.checks` "
         "WHERE run_id = @run AND kind = 'agreement'")
    job = client.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run", "STRING", run_id)]))
    out = {}
    for r in job.result():
        detail = r.detail if isinstance(r.detail, dict) else json.loads(r.detail or "{}")
        if detail.get("group_id"):
            out[detail["group_id"]] = {"check_id": r.check_id, "candidate_id": r.candidate_id}
    return out


def merged_groups(universities: List[dict], providers: Sequence[str], samples: int) -> List[dict]:
    """The week 3 groups of every university, from the cache only (no calls)."""
    props, runs = {}, {}
    for u in universities:
        props[u["qid"]], runs[u["qid"]] = agreement.collect(u["qid"], u["name"], u["website"], providers,
                                                            samples, None, cache_only=True)
        missing = [agreement.run_label(r["provider"], r["sample"]) for r in runs[u["qid"]] if r["missing_from_cache"]]
        if missing:
            raise SystemExit(f"{u['qid']}: week 3 answers not in the cache: {missing}. "
                             "The merged list would differ from week 3.")
    return agreement.analyze_all(universities, props, runs, providers, samples)["groups"]


def cached_ask(cache_only: bool, level: str = "school"):
    """The judge call. With cache_only, a run with no cache file is not asked (None)."""
    from wikidata_discover import llm_helpers
    from wikidata_discover.llm_helpers import LLMHelper

    def ask(label, names, judge, sample, order):
        if cache_only:
            llm_helpers.last_judge_call_id = None
            key = llm_helpers.judge_cache_key(judge, label, names, sample, level, order)
            if not (llm_helpers._CACHE_DIR / f"{key}.json").exists():
                return None
        return LLMHelper.judge_sampled(label, names, judge, sample, order, level)
    return ask


def run(args) -> Path:
    from wikidata_discover import config, run_log
    from wikidata_discover.batch import RESULTS_DIR, operator_identity, source_identity
    from wikidata_discover.cli import run_config
    console = config.console
    judges = list(args.judges or PROVIDERS)
    if args.no_run_log:
        key_sources = {}
    else:
        key_sources = config.load_missing_keys() or {}
        have = {"openai": config.OPENAI_API_KEY, "anthropic": config.ANTHROPIC_API_KEY, "gemini": config.GOOGLE_API_KEY}
        missing = [j for j in judges if not have[j]]
        if missing:
            raise SystemExit(f"No API key for judge(s) {missing}.")
    universities = agreement.universities_to_run(args.universities)
    run_id = run_log.new_run_id("judge")
    out_dir = Path(args.out) if args.out else RESULTS_DIR / "judge" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ask = cached_ask(args.no_run_log)

    if args.departments:
        log = None
        if not args.no_run_log:
            identity = source_identity(out_dir)
            run_log.start(run_id, command="judge", args={"argv": sys.argv[1:]}, qids=[],
                          who=operator_identity(), git_commit=identity,
                          run_config={**run_config(key_sources), "judges": judges, "level": "department",
                                      "orders": list(ORDERS), "samples_per_order": SAMPLES_PER_ORDER})
            log = run_log.current()
            if log is not None:
                log.attach_source_patch(out_dir, identity)
        outcome = "failed"
        try:
            tables = run_departments(args, judges, out_dir, log)
            outcome = "ok"
        finally:
            if log is not None:
                console.print(f"[dim]{run_log.describe(log.finish(outcome, {'out_dir': str(out_dir)}))}[/dim]")
        print_summary(tables, out_dir / "departments")
        return out_dir

    if args.hand_list:
        hand = read_hand_list(Path(args.hand_list))
        known = {u["qid"]: u for u in agreement.universities_to_run(list(hand))}
        log = None
        if not args.no_run_log:
            identity = source_identity(out_dir)
            run_log.start(run_id, command="judge", args={"argv": sys.argv[1:]}, qids=list(hand),
                          who=operator_identity(), git_commit=identity,
                          run_config={**run_config(key_sources), "judges": judges, "hand_list": str(args.hand_list)})
            log = run_log.current()
            if log is not None:
                log.attach_source_patch(out_dir, identity)
        outcome = "failed"
        try:
            rows = judge_hand_list(hand, known, judges, ask)
            write_csv(out_dir / "hand_list_verdicts.csv", rows)
            summary = hand_summary(rows)
            write_csv(out_dir / "hand_list_summary.csv", summary)
            outcome = "ok"
        finally:
            if log is not None:
                console.print(f"[dim]{run_log.describe(log.finish(outcome, {'out_dir': str(out_dir)}))}[/dim]")
        for s in summary:
            console.print(f"  {s['judge']:<10} fake removed {s['fake_removed']}/{s['fake_verdicts']}, "
                          f"real kept {s['real_kept']}/{s['real_verdicts']}"
                          + (f", known wrong removed {s['known_wrong_removed']}/{s['known_wrong_verdicts']}"
                             if s["known_wrong_verdicts"] else "")
                          + (f"; kept a fake: {s['fakes_kept_at_least_once']}" if s["fakes_kept_at_least_once"] else ""))
        console.print(f"\nReport: {out_dir}")
        return out_dir

    groups = merged_groups(universities, PROVIDERS, agreement.DEFAULT_SAMPLES)
    by_qid: Dict[str, List[dict]] = {}
    for g in groups:
        by_qid.setdefault(g["university_qid"], []).append(g)
    log, week3 = None, {}
    if not args.no_run_log:
        identity = source_identity(out_dir)
        run_log.start(run_id, command="judge", args={"argv": sys.argv[1:]},
                      qids=[u["qid"] for u in universities], who=operator_identity(), git_commit=identity,
                      run_config={**run_config(key_sources), "judges": judges, "orders": list(ORDERS),
                                  "samples_per_order": SAMPLES_PER_ORDER, "week3_run": WEEK3_RUN})
        log = run_log.current()
        if log is not None:
            log.attach_source_patch(out_dir, identity)
    console.print(f"[bold]Judge run {run_id}: {len(universities)} universities, judges {judges}, "
                  f"{len(RUNS)} runs each[/bold]")
    lists, verdicts, runs, outcome = [], [], [], "failed"
    try:
        if log is not None:     # inside the try, so a failed lookup still writes the runs row
            week3 = week3_agreement_checks()
            console.print(f"[dim]{len(week3)} week 3 agreement checks found for the judge checks[/dim]")
        for u in universities:
            if log is not None:
                log.set_university(u["qid"])
            l, v, r = judge_university(u, by_qid.get(u["qid"], []), judges, ask, log, week3)
            lists += l
            verdicts += v
            runs += r
            if log is not None:
                log.flush()
            silent = [run_name(x["judge"], x["order"], x["sample"]) for x in r if not x["answered"]]
            console.print(f"  {u['qid']} {u['name']}: {len(by_qid.get(u['qid'], []))} schools"
                          + (f"; no answer from {silent}" if silent else ""))
        tables = analyze(groups, verdicts, judges, {u["qid"]: len(u["schools"]) for u in universities},
                         {u["qid"]: u["set"] for u in universities})
        write_csv(out_dir / "merged_lists.csv", lists)
        write_csv(out_dir / "verdicts.csv", verdicts, VERDICT_COLUMNS)
        write_csv(out_dir / "judge_runs.csv", runs, RUN_COLUMNS)
        for name in ("flip_rates", "self_preference", "precision_recall"):
            write_csv(out_dir / f"{name}.csv", tables[name])
        outcome = "ok"
    finally:
        if log is not None:
            status = log.finish(outcome, {"qids_done": sorted({x["university_qid"] for x in runs}),
                                          "out_dir": str(out_dir)})
            console.print(f"[dim]{run_log.describe(status)}[/dim]")
    print_summary(tables, out_dir)
    return out_dir


def print_summary(tables: Dict[str, List[dict]], out_dir: Path) -> None:
    from wikidata_discover import config
    console = config.console
    console.print("\n[bold]Flip rates (all universities)[/bold]")
    for r in tables["flip_rates"]:
        if r["university_set"] == "all":
            console.print(f"  {r['judge']:<10} same order {r['same_order_flip_rate']!s:<7} "
                          f"cross order {r['cross_order_flip_rate']!s:<7} order effect {r['order_effect']}")
    console.print("\n[bold]Precision and recall (all universities)[/bold]")
    for r in tables["precision_recall"]:
        if r["university_set"] == "all" and r["run"] in ("", "mean", "majority_3_of_4"):
            console.print(f"  {r['method']:<16} {r['run']:<16} kept {r['schools_kept']!s:<6} "
                          f"precision {r['precision']!s:<7} recall {r['recall']}")
    console.print(f"\nReport: {out_dir}")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="The LLM judge, tested (Shuo, week 4)")
    parser.add_argument("--judges", nargs="+", choices=PROVIDERS)
    parser.add_argument("--universities", nargs="+", help="QIDs (default: every university with ground truth)")
    parser.add_argument("--out", help="output folder (default results/judge/<run_id>)")
    parser.add_argument("--no-run-log", action="store_true",
                        help="write no run log rows and make no LLM calls: a run with no cache file "
                             "is reported as not answered")
    parser.add_argument("--hand-list", help="check 1: a CSV (university_qid, name, fake) to judge instead")
    parser.add_argument("--departments", action="store_true",
                        help="judge the merged department lists of the department ground truth schools")
    parser.add_argument("--dept-truth", help=f"department ground truth (default {DEPT_TRUTH.name})")
    parser.add_argument("--dept-schools", help=f"school QIDs of the ground truth (default {DEPT_SCHOOLS.name})")
    parser.add_argument("--dept-samples", type=int, default=DEPT_SAMPLES, help="lists per provider (default 3)")
    parser.add_argument("--subunits-dir", help="folder of the subunits CSV files (default results/subunits)")
    parser.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"), help="check that two runs gave the same report")
    args = parser.parse_args(argv)
    if args.dept_samples < 1:
        parser.error("--dept-samples must be 1 or more")
    if args.compare:
        differ = compare_dirs(Path(args.compare[0]), Path(args.compare[1]))
        print("Identical: every report file is the same." if not differ else f"Different: {differ}")
        sys.exit(1 if differ else 0)
    run(args)


if __name__ == "__main__":
    main()
