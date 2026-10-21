"""Gap report (Anya, week 3): what Wikidata has for each university, next to what the LLM
proposed in run us-tier1 and what the old crowd table has. One row per university.

Sends no query to Wikidata. It reads:
- the snapshots in gs://academiabot/runs/<snapshot run>/snapshots/ (units on Wikidata);
- the us-tier1 candidates rows: the legacy dry-run file, or BigQuery after the import;
- the crowd table nyu-datasets.academiabot.organization (BigQuery).

The crowd table has no QID. A crowd university (a row with no parent) is matched to a
university by its website domain against the snapshot's P856 values, else by its exact
normalized name against the English label and other names. A match counts only when it
is unique on both sides; crowd_match says which rule matched.

gap = llm_proposed - wikidata_direct: positive when the LLM names more schools than
Wikidata links directly to the university, negative when Wikidata has more.

    python -m wikidata_discover.gap_report --candidates <dry-run candidates.jsonl>
    python -m wikidata_discover.gap_report --candidates bigquery
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

from wikidata_discover import config, run_log

COLUMNS = ["qid", "label", "snapshot", "wikidata_units", "wikidata_direct", "llm_proposed",
           "linked", "orphan", "missing", "unresolved", "gap", "crowd_id", "crowd_name",
           "crowd_match", "crowd_units", "crowd_direct"]
STATUSES = ("linked", "orphan", "missing", "unresolved")


def domain(url: Optional[str]) -> Optional[str]:
    """'http://www.Princeton.edu/main/' -> 'princeton.edu'. None for an empty value."""
    if not url:
        return None
    url = url.strip()
    host = urlparse(url if "://" in url else "http://" + url).hostname or ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host or None


def norm_name(name: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower().replace("&", " and ")).strip()


def snapshot_counts(snap: dict) -> Dict[str, object]:
    root = snap["university_qid"]
    units = [u for q, u in snap["units"].items() if u.get("in_tree") and q != root]
    return {"wikidata_units": len(units), "wikidata_direct": sum(1 for u in units if u.get("depth") == 1)}


def crowd_tree(rows: Iterable[dict]) -> Dict[int, dict]:
    """{root id: {name, website, units, direct}} for every crowd row with no parent.
    units counts every level below the root; direct counts its children."""
    rows = list(rows)
    children = defaultdict(list)
    for r in rows:
        if r.get("parent_id") is not None:
            children[r["parent_id"]].append(r["id"])
    out = {}
    for r in rows:
        if r.get("parent_id") is None:
            seen, stack = set(), list(children[r["id"]])
            while stack:
                n = stack.pop()
                if n not in seen:
                    seen.add(n)
                    stack.extend(children[n])
            out[r["id"]] = {"name": r.get("name"), "website": r.get("website_url"),
                            "units": len(seen), "direct": len(children[r["id"]])}
    return out


def match_crowd(snapshots: Dict[str, dict], roots: Dict[int, dict]) -> Dict[str, tuple]:
    """{qid: (crowd id, rule)}. A key that points to more than one university or to more
    than one crowd row is not used, so no match is a guess."""
    def index(keys_of):
        by_key = defaultdict(set)
        for qid, snap in snapshots.items():
            for k in keys_of(snap["units"][snap["university_qid"]]):
                if k:
                    by_key[k].add(qid)
        return by_key

    web = index(lambda u: {domain(e.get("value")) for e in u.get("P856", [])})
    names = index(lambda u: {norm_name(u.get("labels", {}).get("en"))} |
                  {norm_name(a) for a in u.get("aliases", {}).get("en", [])})
    crowd_web, crowd_name = defaultdict(set), defaultdict(set)
    for cid, r in roots.items():
        if domain(r["website"]):
            crowd_web[domain(r["website"])].add(cid)
        if norm_name(r["name"]):
            crowd_name[norm_name(r["name"])].add(cid)

    out: Dict[str, tuple] = {}
    used = set()
    for rule, ours, theirs, key_of in (("website", web, crowd_web, lambda r: domain(r["website"])),
                                       ("name", names, crowd_name, lambda r: norm_name(r["name"]))):
        for cid, r in sorted(roots.items()):
            k = key_of(r)
            if cid in used or not k or len(theirs.get(k, ())) != 1 or len(ours.get(k, ())) != 1:
                continue
            qid = next(iter(ours[k]))
            if qid not in out:
                out[qid] = (cid, rule)
                used.add(cid)
    return out


def build_rows(universities: List[List[str]], snapshots: Dict[str, dict], candidates: Iterable[dict],
               crowd_roots: Dict[int, dict]) -> List[dict]:
    """One row per university, in list order. Pure."""
    counts: Dict[str, Counter] = defaultdict(Counter)
    for c in candidates:
        counts[c["university_qid"]][c["status"]] += 1
    crowd = match_crowd(snapshots, crowd_roots)
    rows = []
    for qid, label in universities:
        snap = snapshots.get(qid)
        cnt = counts.get(qid, Counter())
        row = {"qid": qid, "label": label, "snapshot": "yes" if snap else "not yet",
               "llm_proposed": sum(cnt.values()), **{s: cnt.get(s, 0) for s in STATUSES}}
        if snap:
            row.update(snapshot_counts(snap))
            row["gap"] = row["llm_proposed"] - row["wikidata_direct"]
        if qid in crowd:
            cid, rule = crowd[qid]
            row.update({"crowd_id": cid, "crowd_name": crowd_roots[cid]["name"], "crowd_match": rule,
                        "crowd_units": crowd_roots[cid]["units"], "crowd_direct": crowd_roots[cid]["direct"]})
        rows.append({k: row.get(k) for k in COLUMNS})
    return rows


def write_csv(rows: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


# ── reading (I/O) ────────────────────────────────────────────────────────

def read_snapshots(bucket, snapshot_run: str) -> Dict[str, dict]:
    blobs = [b for b in bucket.client.list_blobs(bucket, prefix=f"runs/{snapshot_run}/snapshots/")
             if b.name.endswith(".json")]
    with ThreadPoolExecutor(16) as ex:
        snaps = list(ex.map(lambda b: json.loads(b.download_as_text()), blobs))
    return {s["university_qid"]: s for s in snaps}


def read_candidates(source: str, bq) -> List[dict]:
    if source == "bigquery":
        q = (f"SELECT university_qid, status FROM `{run_log.table_ref('candidates')}` "
             "WHERE run_id = 'us-tier1' AND legacy")
        return [dict(r) for r in bq.query(q).result()]
    return [json.loads(l) for l in Path(source).read_text().splitlines() if l.strip()]


def read_crowd(bq) -> Dict[int, dict]:
    q = "SELECT id, name, parent_id, website_url FROM `nyu-datasets.academiabot.organization`"
    return crowd_tree(dict(r) for r in bq.query(q).result())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Gap report: Wikidata vs LLM vs crowd table, per university")
    p.add_argument("--candidates", required=True,
                   help="'bigquery' (imported us-tier1 rows) or a candidates.jsonl from the import dry run")
    p.add_argument("--snapshot-run", default="snapshot-us-tier1")
    args = p.parse_args(argv)
    from google.cloud import bigquery, storage
    from wikidata_discover.batch import operator_identity, source_identity
    bucket = storage.Client(project=config.GCP_PROJECT).bucket(config.RUNLOG_BUCKET)
    bq = bigquery.Client(project=config.GCP_PROJECT)
    run_id = run_log.new_run_id("gap-report")
    run_dir = config.RESULTS_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = run_log.start(run_id, command="gap-report", args={"argv": ["gap-report"] + list(argv or sys.argv[1:])},
                        who=operator_identity(), git_commit=source_identity(run_dir), bucket=bucket, bq_client=bq,
                        run_config={"snapshot_run": args.snapshot_run, "candidates": args.candidates})
    outcome, summary = "failed", {}
    try:
        universities = json.loads(bucket.blob("universities_us_tier1.json").download_as_text())
        snapshots = read_snapshots(bucket, args.snapshot_run)
        rows = build_rows(universities, snapshots, read_candidates(args.candidates, bq), read_crowd(bq))
        out = run_dir / "gap_report.csv"
        write_csv(rows, out)
        write_csv(rows, config.RESULTS_DIR / "gap_report.csv")
        where = log.save_text("gap_report.csv", out.read_text(), "text/csv")
        summary = {"universities": len(rows), "with_snapshot": sum(r["snapshot"] == "yes" for r in rows),
                   "crowd_matched": sum(r["crowd_id"] is not None for r in rows),
                   "crowd_match_rules": dict(Counter(r["crowd_match"] for r in rows if r["crowd_match"])),
                   "csv": where}
        outcome = "ok" if summary["with_snapshot"] == len(rows) else "partial"
    finally:
        status = log.finish(outcome, summary)
    print(json.dumps(summary, indent=1))
    print(f"Local copy: {config.RESULTS_DIR / 'gap_report.csv'}")
    print(run_log.describe(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
