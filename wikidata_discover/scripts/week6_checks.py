"""Week 6 checks 1 and 2 (Anya), repeatable.

    python -m wikidata_discover.scripts.week6_checks ground-truth [--file results/hierarchy/Q49210.json]
    python -m wikidata_discover.scripts.week6_checks baseline [--decide choose|yes-no]

ground-truth (check 1): every unit in Shuo's ground truth (eval/dept_ground_truth.csv) that
has a QID must link to that QID in NYU's hierarchy file. Writes
results/week6/check1_ground_truth.csv, one row per ground-truth unit: the node found (same
school, name fits), its QID and alignment, and the result (right, wrong QID, no QID, unit
not in the file). Rows with qid "none" are listed too: a node there must not have a QID.

baseline (check 2): the 20 "missing" schools of the week 3 baseline
(results/check4_missing_sample.csv) go through every week 6 search and the decision. Writes
results/week6/check2_baseline.csv with the QID found and the week 3 label. Spends a little
LLM credit (one decision per school; cached after the first run).
"""
import argparse
import csv
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from wikidata_discover import alignment, config, hierarchy_file as hf

GROUND_TRUTH = Path(__file__).resolve().parent.parent / "eval" / "dept_ground_truth.csv"
BASELINE = config.RESULTS_DIR / "check4_missing_sample.csv"
OUT = config.RESULTS_DIR / "week6"
SCHOOLS = {"Stern": "Q770467", "Courant": "Q946028", "Steinhardt": "Q966540"}
_STRIP = r"^(department of|dept\.? of|division of|program in|the)\s+"


def core(name: str) -> str:
    """The name without "Department of" and similar words in front, as Shuo's scorer does. Pure."""
    s = (name or "").strip()
    while True:
        t = re.sub(_STRIP, "", s, flags=re.I).strip()
        if t == s:
            return s
        s = t


def find_node(nodes: List[dict], school_qid: str, name: str) -> Optional[dict]:
    """The node under that school whose name (or an alias) fits the ground-truth name. Pure."""
    kids = [n for n in nodes if any(p.get("qid") == school_qid for p in n.get("parents_shown") or [])]
    for n in kids:
        for cand in [n["name"]] + (n.get("aliases") or []):
            if hf.names_fit(core(name), core(cand), [n["parents_shown"][0].get("name") or "", "New York University", "NYU"]):
                return n
    return None


def ground_truth_rows(nodes: List[dict], truth: List[dict]) -> List[dict]:
    out = []
    for t in truth:
        want = (t.get("qid") or "").strip()
        n = find_node(nodes, SCHOOLS.get(t["parent"], ""), t["name"])
        got = n.get("qid") if n else None
        if want.lower() == "none":
            result = "right (none)" if n and not got else ("unit not in the file" if not n else f"has a QID: {got}")
        elif not want:
            result = "not checked in the ground truth"
        elif not n:
            result = "unit not in the file"
        elif got == want:
            result = "right"
        elif got:
            result = "wrong QID"
        else:
            result = "no QID"
        out.append({"school": t["parent"], "ground_truth_name": t["name"], "ground_truth_qid": want,
                    "node_id": n["node_id"] if n else "", "node_name": n["name"] if n else "",
                    "node_qid": got or "", "alignment": n["alignment"] if n else "",
                    "alignment_reason": (n.get("alignment_reason") or "")[:200] if n else "", "result": result})
    return out


def write(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def baseline_rows(sample: List[dict], decide: str, searcher=None, choose_fn=None, yes_no_fn=None) -> List[dict]:
    searcher = searcher or alignment.Searcher()
    out = []
    for r in sample:
        url = (r.get("source_url (the LLM website field; us-tier1 kept no other URL)") or "").strip()
        unit = {"name": r["school_name"], "unit_type": r["unit_type_llm"], "parent_name": r["university_name"],
                "university": r["university_name"], "website": url if url.startswith("http") else None,
                "source_url": None, "aliases": []}
        a = alignment.align_unit(unit, searcher, decide, exclude={r["university_qid"]},
                                 choose_fn=choose_fn, yes_no_fn=yes_no_fn)
        label = r["already on Wikidata (QID)"]
        want = re.search(r"Q\d+", label).group(0) if re.search(r"Q\d+", label) else ""
        if want:
            result = "right" if a["qid"] == want else ("wrong QID" if a["qid"] else f"not found ({a['status']})")
        else:
            result = "right (new)" if a["status"] == "new" else (
                f"QID given ({a['qid']}); week 3 said not on Wikidata: check by hand" if a["qid"] else a["status"])
        out.append({"sample_no": r["sample_no"], "school": r["school_name"], "university": r["university_name"],
                    "week3_label": label, "week3_really_a_school": r["really a school (yes/no)"],
                    "status": a["status"], "qid": a["qid"] or "",
                    "found_by": "; ".join(",".join(c["found_by"]) for c in a["candidates"] if c["chosen"]),
                    "candidates": len(a["candidates"]), "reason": a["reason"][:200], "result": result})
    return out


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("ground-truth")
    g.add_argument("--file", default=str(config.RESULTS_DIR / "hierarchy" / "Q49210.json"))
    b = sub.add_parser("baseline")
    b.add_argument("--decide", choices=alignment.DECIDE_MODES, default="choose")
    args = p.parse_args(argv)
    if args.cmd == "ground-truth":
        nodes = json.loads(Path(args.file).read_text())["nodes"]
        rows = ground_truth_rows(nodes, list(csv.DictReader(GROUND_TRUTH.open())))
        write(OUT / "check1_ground_truth.csv", rows)
        with_qid = [r for r in rows if r["ground_truth_qid"] and r["ground_truth_qid"].lower() != "none"]
        config.console.print(f"Check 1: {sum(r['result'] == 'right' for r in with_qid)} of {len(with_qid)} units "
                             f"with a QID link to it. Misses: {[r['ground_truth_name'] for r in with_qid if r['result'] != 'right']}")
    else:
        config.load_api_keys()
        rows = baseline_rows(list(csv.DictReader(BASELINE.open())), args.decide)
        write(OUT / f"check2_baseline_{args.decide}.csv", rows)
        config.console.print(f"Check 2: {sum(r['result'].startswith('right') for r in rows)} of {len(rows)} right. "
                             f"Wrote {OUT}")


if __name__ == "__main__":
    main()
