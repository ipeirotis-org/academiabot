"""Research question A1 (Anya, week 6): how often is a "new" unit already on Wikidata, and
which search finds it?

    python -m wikidata_discover.a1_eval run              # searches and decisions (spends LLM credit)
    python -m wikidata_discover.a1_eval score --labels a1_labels_anya.csv

Inputs (from gs://academiabot/a1/, drawn on 2026-11-18 with seed 20261118):
- a1_labeling_sheet.csv: the 150 units as the labeler saw them.
- a1_pipeline_answers.csv: the stratum and the old pipeline's answer (run us-tier1).
- the labels: the right QID (my_qid, second_qid when Wikidata has two items) or "none".

`run` searches each unit once with every search kind (responses saved with the run),
then decides for each candidate set below, both ways (choose from the list; yes or no per
item). The tree search uses the university's week 3 snapshot (snapshot-us-tier1), because a
fresh snapshot for 140 universities would take hours. Candidate sets:
- alone: prefix only, and prefix plus one other kind (fulltext, university_name,
  other_names, website, tree)
- cumulative: the kinds added one at a time in the order of alignment.SEARCH_KINDS
Identical sets share one decision (the cache key is the prompt).

`score` computes, for each method (before = us-tier1, and each set and way to decide):
- false-new rate: of the units the method calls new, the share that have a QID;
- false-link rate: of the units the method gives a QID, the share given a wrong one
  (a link to any one of the labeled duplicates counts as right);
per stratum and overall. The overall rate is weighted: each unit has the weight
(units in its stratum in us-tier1) / (units sampled from it); the weights are summed in the
numerator and in the denominator separately. Stratum rates are never averaged.
Units a method leaves uncertain are in neither denominator; their count is reported.
"before" for the 41 linked rows uses an answer rebuilt with the old fuzzy match against the
week 3 snapshot, because us-tier1 did not save which item a linked unit matched. Every row
that uses it says "approximate (rebuilt)". The 9 orphans have the old QID, so they also get
a "before" row of their own.
"""
import argparse
import concurrent.futures
import csv
import io
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from wikidata_discover import alignment, config, hierarchy_file as hf
from wikidata_discover.discovery import is_fuzzy_match

logger = logging.getLogger(__name__)

BUCKET_DIR = "a1"
SNAPSHOT_RUN = "snapshot-us-tier1"
STRATUM_SIZES = {"missing": 6413, "linked_or_orphan": 2777}
OUT_DIR = config.RESULTS_DIR / "a1"
APPROX = "approximate (rebuilt)"


def candidate_sets() -> List[Tuple[str, Tuple[str, ...]]]:
    """(name, kinds) for every row of the report, in report order. Pure."""
    k = alignment.SEARCH_KINDS
    out = [("alone: prefix", ("prefix",))]
    out += [(f"alone: prefix + {x}", ("prefix", x)) for x in k[1:]]
    out += [("cumulative: " + " + ".join(k[:i]), tuple(k[:i])) for i in range(2, len(k) + 1)]
    return out


def distinct_sets() -> List[Tuple[str, ...]]:
    seen = []
    for _, kinds in candidate_sets():
        s = tuple(sorted(kinds, key=alignment.SEARCH_KINDS.index))
        if s not in seen:
            seen.append(s)
    return seen


# ── inputs ───────────────────────────────────────────────────────────────

def read_csv_text(text: str) -> List[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def load_inputs(bucket) -> Tuple[List[dict], Dict[str, dict]]:
    sheet = read_csv_text(bucket.blob(f"{BUCKET_DIR}/a1_labeling_sheet.csv").download_as_text())
    answers = {r["item_id"]: r for r in read_csv_text(bucket.blob(f"{BUCKET_DIR}/a1_pipeline_answers.csv").download_as_text())}
    return sheet, answers


def load_snapshot(bucket, qid: str) -> Optional[dict]:
    blob = bucket.blob(f"runs/{SNAPSHOT_RUN}/snapshots/{qid}.json")
    return json.loads(blob.download_as_bytes()) if blob.exists() else None


def tree_matches(name: str, snap: Optional[dict]) -> List[str]:
    """Items in the university's week 3 snapshot whose name fits, under any parent. Pure."""
    if not snap:
        return []
    root = snap["units"].get(snap["university_qid"]) or {}
    prefixes = [snap.get("university_label") or ""] + hf.unit_aliases(root)
    return [c["qid"] for c in hf.elsewhere_matches(name, snap, None, prefixes)]


def rebuilt_linked_answer(name: str, snap: Optional[dict]) -> Optional[str]:
    """The item a us-tier1 "linked" unit most likely matched: the first direct child of
    the university in the week 3 snapshot whose label fits by the old fuzzy match.
    APPROXIMATE: us-tier1 did not save the item. Pure."""
    if not snap:
        return None
    kids = sorted(q for q, u in snap["units"].items() if u.get("depth") == 1 and u.get("in_tree"))
    for q in kids:
        u = snap["units"][q]
        if any(is_fuzzy_match(name, n) for n in [u.get("label") or ""] + hf.unit_aliases(u)):
            return q
    return None


# ── run ──────────────────────────────────────────────────────────────────

def unit_of(row: dict) -> dict:
    url = row.get("website_or_source_url") or None
    return {"name": row["unit_name"], "unit_type": row.get("unit_type") or "", "parent_name": row["university"],
            "university": row["university"], "website": url, "source_url": None, "aliases": []}


def run(bucket, log, workers: int = 4, out_dir: Path = None, searcher=None, choose_fn=None, yes_no_fn=None,
        snapshots=None) -> Path:
    """Search every unit, then decide every distinct candidate set both ways. Writes
    a1_decisions.jsonl (one line per unit, set, and way to decide) and returns its path."""
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet, answers = load_inputs(bucket)
    searcher = searcher or alignment.Searcher(log=log)
    snaps = snapshots if snapshots is not None else {}
    found_by_item = {}
    # Phase 1, one at a time (polite to Wikidata): every search, and the candidates' details.
    for i, row in enumerate(sheet, 1):
        uq = row["university_qid"]
        if uq not in snaps:
            snaps[uq] = load_snapshot(bucket, uq)
        u = unit_of(row)
        found, errors = searcher.run(u, lambda x, s=snaps[uq]: tree_matches(x["name"], s))
        details = searcher.details([q for v in found.values() for q in v])
        found_by_item[row["item_id"]] = (u, found, errors, details)
        if i % 10 == 0:
            config.console.print(f"A1 searches: {i} of {len(sheet)} units")
    # Phase 2, in parallel: the decisions (LLM calls; identical prompts are cached).
    jobs = [(item, kinds, mode) for item in found_by_item for kinds in distinct_sets() for mode in alignment.DECIDE_MODES]

    def one(job):
        item, kinds, mode = job
        u, found, errors, details = found_by_item[item]
        r = alignment.align_unit(u, searcher, mode, kinds, found=found, errors=errors, details=details,
                                 exclude={answers[item]["university_qid"]}, choose_fn=choose_fn, yes_no_fn=yes_no_fn)
        return {"item_id": item, "kinds": list(kinds), "mode": mode, "status": r["status"], "qid": r["qid"],
                "duplicates": r["duplicates"], "reason": r["reason"], "candidates": r["candidates"],
                "search_errors": r["search_errors"], "llm_call_ids": r["llm_call_ids"], "provider": r["provider"]}
    path = out_dir / "a1_decisions.jsonl"
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for n, res in enumerate(pool.map(one, jobs), 1):
            rows.append(res)
            if n % 200 == 0:
                config.console.print(f"A1 decisions: {n} of {len(jobs)}")
    with path.open("w") as f:
        for item, (u, found, errors, details) in found_by_item.items():
            f.write(json.dumps({"item_id": item, "searches": found, "search_errors": errors}) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    before = {item: {"stratum": answers[item]["stratum"], "status": answers[item]["status"],
                     "matched_qid": answers[item]["matched_qid"] or None,
                     "rebuilt_qid": rebuilt_linked_answer(answers[item]["name"], snaps.get(answers[item]["university_qid"]))
                     if answers[item]["status"] == "linked" else None} for item in answers}
    (out_dir / "a1_before.json").write_text(json.dumps(before, indent=1))
    return path


# ── score (pure) ─────────────────────────────────────────────────────────

def parse_label(row: dict) -> Optional[List[str]]:
    """[QIDs] for a labeled unit, [] for "none", None when the row is not labeled yet. Pure."""
    vals = [(row.get("my_qid") or "").strip(), (row.get("second_qid") or "").strip()]
    if not vals[0]:
        return None
    if vals[0].lower() == "none":
        return []
    return [v.upper() for v in vals if v and v.lower() != "none"]


def rates(answers: Dict[str, Optional[str]], status: Dict[str, str], labels: Dict[str, List[str]],
          strata: Dict[str, str], sampled: Dict[str, int], sizes: Dict[str, int] = None) -> dict:
    """False-new and false-link rates per stratum and weighted overall. answers: item ->
    QID or None; status: item -> new, aligned, or uncertain. Pure."""
    sizes = sizes or STRATUM_SIZES
    w = {s: sizes[s] / sampled[s] for s in sampled if sampled[s]}
    out = {}
    groups = {s: [i for i in labels if strata[i] == s] for s in sampled}
    groups["overall (weighted)"] = list(labels)
    for g, items in groups.items():
        weight = (lambda i: w[strata[i]]) if g == "overall (weighted)" else (lambda i: 1.0)
        new = [i for i in items if status[i] == "new"]
        linked = [i for i in items if status[i] == "aligned"]
        fn_num = sum(weight(i) for i in new if labels[i])
        fn_den = sum(weight(i) for i in new)
        fl_num = sum(weight(i) for i in linked if answers[i] not in labels[i])
        fl_den = sum(weight(i) for i in linked)
        out[g] = {"false_new_rate": fn_num / fn_den if fn_den else None, "called_new": len(new),
                  "false_new": sum(1 for i in new if labels[i]),
                  "false_link_rate": fl_num / fl_den if fl_den else None, "given_qid": len(linked),
                  "false_link": sum(1 for i in linked if answers[i] not in labels[i]),
                  "uncertain": sum(1 for i in items if status[i] == "uncertain"), "labeled": len(items)}
    return out


def before_method(before: Dict[str, dict], items: List[str]) -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    """The old pipeline's answers: missing -> new; orphan -> its QID; linked -> the rebuilt
    QID (approximate), or uncertain when nothing could be rebuilt. Pure."""
    ans, st = {}, {}
    for i in items:
        b = before[i]
        if b["status"] == "missing":
            ans[i], st[i] = None, "new"
        elif b["status"] == "orphan":
            ans[i], st[i] = b["matched_qid"], "aligned"
        else:
            q = b.get("rebuilt_qid")
            ans[i], st[i] = q, ("aligned" if q else "uncertain")
    return ans, st


def score(labels_rows: List[dict], answers_rows: List[dict], decisions: List[dict], before: Dict[str, dict]) -> List[dict]:
    """The report rows: one per method and group. Pure."""
    strata = {r["item_id"]: r["stratum"] for r in answers_rows}
    labels = {r["item_id"]: parse_label(r) for r in labels_rows}
    labels = {i: v for i, v in labels.items() if v is not None}
    sampled = {s: sum(1 for i in strata if strata[i] == s) for s in STRATUM_SIZES}
    report = []

    def add(method, note, ans, st, items=None):
        items = items or list(labels)
        sub = {i: labels[i] for i in items if i in labels}
        for g, r in rates(ans, st, sub, strata, sampled).items():
            if r["labeled"]:
                report.append({"method": method, "note": note, "group": g, **r})

    ans, st = before_method(before, list(labels))
    add("before (us-tier1)", f"linked rows: {APPROX}", ans, st)
    orphans = [i for i in labels if before[i]["status"] == "orphan"]
    add("before (us-tier1), orphans only", "the old QID was saved", ans, st, orphans)

    by = {}
    for d in decisions:
        if "mode" in d:
            by[(tuple(d["kinds"]), d["mode"], d["item_id"])] = d
    for name, kinds in candidate_sets():
        key = tuple(sorted(kinds, key=alignment.SEARCH_KINDS.index))
        for mode in alignment.DECIDE_MODES:
            ds = {i: by.get((key, mode, i)) for i in labels}
            if any(v is None for v in ds.values()):
                continue
            add(f"{name} [{mode}]", "", {i: d["qid"] for i, d in ds.items()}, {i: d["status"] for i, d in ds.items()})
    return report


def search_recall(labels_rows: List[dict], decisions: List[dict]) -> List[dict]:
    """For each search kind: of the labeled units that have a QID, how many the kind found
    (the labeled QID was in its results). Pure."""
    labels = {r["item_id"]: parse_label(r) for r in labels_rows}
    found = {d["item_id"]: d["searches"] for d in decisions if "searches" in d}
    has = [i for i, v in labels.items() if v]
    out = []
    for k in alignment.SEARCH_KINDS:
        hit = [i for i in has if i in found and set(found[i].get(k) or []) & set(labels[i])]
        out.append({"search": k, "units_with_a_qid": len(has), "found_by_this_search": len(hit),
                    "only_this_search": sum(1 for i in hit if not any(set(found[i].get(o) or []) & set(labels[i])
                                                                      for o in alignment.SEARCH_KINDS if o != k))})
    return out


def duplicate_flags(labels_rows: List[dict], decisions: List[dict]) -> List[dict]:
    """Units labeled with two items: did each method flag the duplicate? Pure."""
    labels = {r["item_id"]: parse_label(r) for r in labels_rows}
    two = {i for i, v in labels.items() if v and len(v) >= 2}
    out: Dict[str, dict] = {}
    for d in decisions:
        if "mode" in d and d["item_id"] in two:
            k = f"{' + '.join(d['kinds'])} [{d['mode']}]"
            r = out.setdefault(k, {"method": k, "units_with_two_items": len(two), "flagged": 0})
            r["flagged"] += 1 if set(d.get("duplicates") or []) >= set(labels[d["item_id"]]) else 0
    return list(out.values())


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--workers", type=int, default=4)
    s = sub.add_parser("score")
    s.add_argument("--labels", required=True, help="Labeled sheet (local path)")
    args = p.parse_args(argv)
    from google.cloud import storage
    bucket = storage.Client(project=config.GCP_PROJECT).bucket(config.RUNLOG_BUCKET)
    if args.cmd == "run":
        from wikidata_discover import cli, run_log
        from wikidata_discover.batch import operator_identity, source_identity, RESULTS_DIR
        keys = config.load_api_keys()
        run_id = run_log.new_run_id("a1-eval")
        run_dir = RESULTS_DIR / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        identity = source_identity(run_dir)
        log = run_log.start(run_id, command="a1_eval run", args={"argv": sys.argv[1:]}, qids=[], who=operator_identity(),
                            git_commit=identity, run_config={**cli.run_config(keys), "candidate_sets": distinct_sets(),
                                                              "modes": list(alignment.DECIDE_MODES),
                                                              "tree_snapshot_run": SNAPSHOT_RUN})
        log.attach_source_patch(run_dir, identity)
        outcome = "failed"
        try:
            path = run(bucket, log, args.workers, out_dir=OUT_DIR / run_id)
            outcome = "ok"
        finally:
            config.console.print(run_log.describe(log.finish(outcome)))
        for f in sorted(path.parent.iterdir()):
            bucket.blob(f"{BUCKET_DIR}/{run_id}/{f.name}").upload_from_filename(str(f), if_generation_match=0)
        config.console.print(f"Wrote {path.parent} and gs://{bucket.name}/{BUCKET_DIR}/{run_id}/")
    else:
        latest = sorted(d for d in OUT_DIR.iterdir() if (d / "a1_decisions.jsonl").exists())[-1]
        decisions = [json.loads(l) for l in (latest / "a1_decisions.jsonl").read_text().splitlines()]
        before = json.loads((latest / "a1_before.json").read_text())
        labels = list(csv.DictReader(open(args.labels)))
        answers = read_csv_text(bucket.blob(f"{BUCKET_DIR}/a1_pipeline_answers.csv").download_as_text())
        write_csv(latest / "a1_rates.csv", score(labels, answers, decisions, before))
        write_csv(latest / "a1_search_recall.csv", search_recall(labels, decisions))
        write_csv(latest / "a1_duplicate_flags.csv", duplicate_flags(labels, decisions))
        config.console.print(f"Wrote a1_rates.csv, a1_search_recall.csv, a1_duplicate_flags.csv in {latest}")


if __name__ == "__main__":
    main()
