"""
Numbers for the extraction setup choice (Shuo, week 6; see TASKS.md and SPECS.md).

The choice is which generators list the departments, and which judge filters the merged
list. It uses search-on numbers only:

    dept_generators.csv   each provider as a generator on its own department lists for the six
                          ground-truth schools (week 4 lists, search on for every provider)
    combos.csv            for each level (department, school), each set of generators, and each
                          filter (none, 2 or more providers, each judge's majority of 4 runs):
                          units kept, precision, recall, F1

The judge verdicts come from one judge test per level: departments from the week 4 run
(those lists already had search on), schools from the week 6 rerun on the week 5 search-on
lists (`judge --search-on`). A judge saw the list merged from all three providers. For a
smaller set of generators, the units of that set keep the verdicts the judge gave in the
full list. That is close to, but not the same as, judging the smaller list.

Usage:
    python -m wikidata_discover.eval.setup_choice --departments DIR/departments --schools DIR2 --out OUT
"""

import argparse
import csv
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from wikidata_discover.eval.agreement import PROVIDERS, write_csv

MAJORITY = 3   # kept in 3 or more of the 4 runs of a judge


def _split(v: str) -> List[str]:
    return [x.strip() for x in (v or "").split(";") if x.strip()]


def read_csv(path: Path) -> List[dict]:
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def generator_sets(providers: Sequence[str] = PROVIDERS) -> List[tuple]:
    """Every non-empty set of providers, smallest first. Pure."""
    return [c for n in range(1, len(providers) + 1) for c in combinations(providers, n)]


def judge_keeps(verdicts: Sequence[dict], majority: int = MAJORITY) -> Dict[str, set]:
    """{judge: group ids kept in `majority` or more of its runs}. Pure."""
    votes: Dict[tuple, int] = {}
    for v in verdicts:
        if str(v["kept"]) == "1":
            votes[(v["judge"], v["group_id"])] = votes.get((v["judge"], v["group_id"]), 0) + 1
    out: Dict[str, set] = {}
    for (judge, gid), n in votes.items():
        if n >= majority:
            out.setdefault(judge, set()).add(gid)
    return out


def truth_options(g: dict) -> List[str]:
    """The true units a group can match. groups.csv keeps only the one it matched in the
    full list (truth_name): a correct group, or a duplicate of an already matched unit.
    A `truth_options` column (";" between names) wins when present. Pure."""
    if g.get("truth_options"):
        return _split(g["truth_options"])
    if g.get("truth_name") and (g["status"] == "correct" or g.get("wrong_kind") == "duplicate"):
        return [g["truth_name"]]
    return []


def _score(kept: Sequence[dict], true_count: int) -> dict:
    """The kept groups are matched to the true units again (agreement.matched_count), so a
    kept duplicate counts as correct when the group first matched to its unit was dropped,
    as in the judge's own precision_recall.csv. Pure."""
    from wikidata_discover.eval.agreement import matched_count
    n = matched_count([{"university_qid": g["university_qid"], "status": g["status"],
                        "truth_options": truth_options(g)} for g in kept])
    p = n / len(kept) if kept else None
    r = n / true_count if true_count else None
    f1 = 2 * p * r / (p + r) if p is not None and r is not None and p + r > 0 else (0.0 if kept else None)
    rnd = lambda x: "" if x is None else round(x, 4)  # noqa: E731
    return {"kept": len(kept), "correct": n, "true_found": n, "true_total": true_count,
            "precision": rnd(p), "recall": rnd(r), "f1": rnd(f1)}


def combos(groups: Sequence[dict], verdicts: Sequence[dict], true_counts: Dict[str, int], level: str,
           judges: Sequence[str] = PROVIDERS, unit_sets: Optional[Dict[str, str]] = None) -> List[dict]:
    """One row per (set of universities or schools, generators, filter). Recall counts the
    true units found (a true unit found twice counts once). A judge filter keeps a unit when
    that judge kept it in 3 or more of its 4 runs. The 2+ filter needs 2 or more of the
    chosen generators to name the unit, so it does not apply to one generator.
    unit_sets ({university or school id: set}) puts every requested university or school
    in its set, so one with no groups still adds its true units to that set's recall, as in
    judge.precision_recall (review of pull request 29). Pure."""
    keeps = judge_keeps(verdicts)
    set_of = {g["university_qid"]: g["university_set"] for g in groups}
    set_of.update(unit_sets or {})
    sets = sorted(set(set_of.values())) + ["all"]
    rows = []
    for uset in sets:
        gs = [g for g in groups if uset == "all" or g["university_set"] == uset]
        tc = sum(n for q, n in true_counts.items() if uset == "all" or set_of.get(q) == uset)
        for gens in generator_sets():
            mine = [g for g in gs if set(_split(g["providers"])) & set(gens)]
            filters = [("none", mine)]
            if len(gens) >= 2:
                filters.append(("named by 2+ of these", [g for g in mine if len(set(_split(g["providers"])) & set(gens)) >= 2]))
            for j in judges:
                filters.append((f"{j} judge (3 of 4 runs)", [g for g in mine if g["group_id"] in keeps.get(j, set())]))
            for name, kept in filters:
                rows.append({"level": level, "set": uset, "generators": " + ".join(gens), "filter": name,
                             **_score(kept, tc)})
    return rows


def dept_generators(list_scores: Sequence[dict]) -> List[dict]:
    """Each provider as a generator of department lists: the mean name precision, recall
    and F1 over its lists, per set of schools and overall. Pure."""
    rows = []
    sets = sorted({r["set"] for r in list_scores}) + ["all"]
    for prov in PROVIDERS:
        for uset in sets:
            mine = [r for r in list_scores if r["provider"] == prov and (uset == "all" or r["set"] == uset)
                    and str(r["answered"]) == "True"]
            row = {"provider": prov, "set": uset, "lists": len(mine)}
            for k in ("name_precision", "name_recall", "name_f1"):
                vals = [float(r[k]) for r in mine if r[k] != ""]
                row["mean_" + k.split("_")[1]] = round(sum(vals) / len(vals), 4) if vals else ""
            f1s = [float(r["name_f1"]) for r in mine if r["name_f1"] != ""]
            row["min_f1"] = min(f1s) if f1s else ""
            rows.append(row)
    return rows


def dept_true_counts() -> Tuple[Dict[str, int], Dict[str, str]]:
    """({school id: true departments}, {school id: set}) of every scored school."""
    from wikidata_discover.eval import dept_scorer as ds
    from wikidata_discover.eval.judge import DEPT_TRUTH, load_dept_schools
    truth = ds.load_ground_truth(DEPT_TRUTH)
    schools = load_dept_schools()
    return ({s["id"]: sum(1 for t in truth.rows if t.parent == s["parent"]) for s in schools},
            {s["id"]: s["set"] for s in schools})


def school_true_counts() -> Tuple[Dict[str, int], Dict[str, str]]:
    """({university QID: true schools}, {university QID: set}) of every eval university."""
    from wikidata_discover.eval import agreement
    us = agreement.universities_to_run(None)
    return {u["qid"]: len(u["schools"]) for u in us}, {u["qid"]: u["set"] for u in us}


COMBO_COLUMNS = ["level", "set", "generators", "filter", "kept", "correct", "true_found", "true_total",
                 "precision", "recall", "f1"]


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--departments", required=True, help="the departments folder of the week 4 judge run")
    p.add_argument("--schools", required=True, help="the folder of the week 6 judge run on the search-on lists")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    d, s = Path(a.departments), Path(a.schools)
    counts, sets = dept_true_counts()
    rows = combos(read_csv(d / "groups.csv"), read_csv(d / "verdicts.csv"), counts, "department", unit_sets=sets)
    counts, sets = school_true_counts()
    rows += combos(read_csv(s / "groups.csv"), read_csv(s / "verdicts.csv"), counts, "school", unit_sets=sets)
    write_csv(out / "combos.csv", rows, COMBO_COLUMNS)
    write_csv(out / "dept_generators.csv", dept_generators(read_csv(d / "list_scores.csv")))
    print(f"Wrote {out / 'combos.csv'} and {out / 'dept_generators.csv'}")


if __name__ == "__main__":
    main()
