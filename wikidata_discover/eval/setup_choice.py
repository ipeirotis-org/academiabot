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
from typing import Dict, List, Optional, Sequence

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


def _score(kept: Sequence[dict], true_count: int) -> dict:
    correct = [g for g in kept if g["status"] == "correct"]
    found = {(g["university_qid"], g["truth_name"]) for g in correct}
    p = len(correct) / len(kept) if kept else None
    r = len(found) / true_count if true_count else None
    f1 = 2 * p * r / (p + r) if p is not None and r is not None and p + r > 0 else (0.0 if kept else None)
    rnd = lambda x: "" if x is None else round(x, 4)  # noqa: E731
    return {"kept": len(kept), "correct": len(correct), "true_found": len(found), "true_total": true_count,
            "precision": rnd(p), "recall": rnd(r), "f1": rnd(f1)}


def combos(groups: Sequence[dict], verdicts: Sequence[dict], true_counts: Dict[str, int], level: str,
           judges: Sequence[str] = PROVIDERS) -> List[dict]:
    """One row per (set of universities or schools, generators, filter). Recall counts the
    true units found (a true unit found twice counts once). A judge filter keeps a unit when
    that judge kept it in 3 or more of its 4 runs. The 2+ filter needs 2 or more of the
    chosen generators to name the unit, so it does not apply to one generator. Pure."""
    keeps = judge_keeps(verdicts)
    sets = sorted({g["university_set"] for g in groups}) + ["all"]
    set_of = {g["university_qid"]: g["university_set"] for g in groups}
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


def dept_true_counts() -> Dict[str, int]:
    from wikidata_discover.eval import dept_scorer as ds
    from wikidata_discover.eval.judge import DEPT_TRUTH, load_dept_schools
    truth = ds.load_ground_truth(DEPT_TRUTH)
    return {s["id"]: sum(1 for t in truth.rows if t.parent == s["parent"]) for s in load_dept_schools()}


def school_true_counts() -> Dict[str, int]:
    from wikidata_discover.eval import agreement
    return {u["qid"]: len(u["schools"]) for u in agreement.universities_to_run(None)}


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
    rows = combos(read_csv(d / "groups.csv"), read_csv(d / "verdicts.csv"), dept_true_counts(), "department")
    rows += combos(read_csv(s / "groups.csv"), read_csv(s / "verdicts.csv"), school_true_counts(), "school")
    write_csv(out / "combos.csv", rows, COMBO_COLUMNS)
    write_csv(out / "dept_generators.csv", dept_generators(read_csv(d / "list_scores.csv")))
    print(f"Wrote {out / 'combos.csv'} and {out / 'dept_generators.csv'}")


if __name__ == "__main__":
    main()
