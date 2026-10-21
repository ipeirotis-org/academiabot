"""One-time import of run us-tier1 (2026-10-03) into llm_calls and candidates (Anya, week 3).

us-tier1 ran before the run log, so it kept no prompt, no raw response, and no saved page.
Every imported row has legacy = true and source_file = the original file in the bucket, so
nothing looks more traceable than it is. The import only reads the bucket (never
results/cache) and reads English labels for the orphan QIDs from Wikidata.

Where each part comes from (gs://academiabot/runs/us-tier1/):
- log.jsonl: the last record of each QID names the cache files that university used, its
  provider, invocation, and report counts.
- cache/<key>.json: a list is an extraction answer (every unit proposed, linked ones too);
  {"answer", "provider"} is a match decision. A match file cannot be traced back to its
  unit: its name is a hash of a prompt that held that day's Wikidata choices.
- missing_divisions_<QID>.csv: the missing and orphan rows (orphans with their QID).
- invocations.jsonl: the models of each invocation.

Linked units are the units in the extraction answer that are not in the CSV and not
unresolved. us-tier1 never saved which item a linked unit matched, so their matched_qid
and matched_label stay empty. A university whose counts do not equal its report is not
imported and is listed for a person.

    python -m wikidata_discover.legacy_import            # dry run: rows to local JSON only
    python -m wikidata_discover.legacy_import --write    # load into BigQuery; each table skips ids it has, so a retry is safe
"""
import argparse
import csv
import hashlib
import io
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from wikidata_discover import config, run_log

SOURCE_RUN = "us-tier1"
PREFIX = f"runs/{SOURCE_RUN}/"
LEGACY_CALL_NOTE = ("Legacy import of run us-tier1 (2026-10-03, before the run log). Only the "
                    "parsed answer in the cache file was kept: no prompt and no raw response.")


def gs(name: str, bucket: str = "academiabot") -> str:
    return f"gs://{bucket}/{name}"


def legacy_id(*parts: str) -> str:
    """A stable id, so the same input always gives the same row id."""
    return hashlib.sha256("|".join(("legacy", SOURCE_RUN) + parts).encode()).hexdigest()[:32]


def last_records(log_lines: List[str]) -> Dict[str, dict]:
    """The last log record of each QID (the last record wins, as in the batch runner)."""
    out = {}
    for line in log_lines:
        line = line.strip()
        if line:
            r = json.loads(line)
            out[r["qid"]] = r
    return out


def invocation_models(inv_lines: List[str]) -> Dict[str, dict]:
    out = {}
    for line in inv_lines:
        if line.strip():
            r = json.loads(line)
            if r.get("invocation_id") and r.get("models"):
                out[r["invocation_id"]] = r["models"]
    return out


def kind_of(content) -> str:
    if isinstance(content, list):
        return "extract"
    if isinstance(content, dict) and "answer" in content:
        return "match"
    return "unknown"


def build_university(rec: dict, files: Dict[str, object], csv_text: Optional[str],
                     models: Dict[str, str], bucket: str = "academiabot"
                     ) -> Tuple[List[dict], List[dict], Optional[str]]:
    """Rows for one university. Pure. Returns (llm_calls rows, candidates rows, problem).
    A problem means the candidates are not imported (the llm_calls rows still are)."""
    qid, provider = rec["qid"], rec.get("provider")
    lists = [f for f, c in files.items() if kind_of(c) == "extract"]
    answered = [f for f in lists if files[f]]
    extract_file = answered[0] if len(answered) == 1 else None

    calls, call_id_of = [], {}
    for f in sorted(files):
        kind = kind_of(files[f])
        prov = files[f].get("provider") if kind == "match" else (provider if f == extract_file else None)
        cid = legacy_id(qid, f)
        call_id_of[f] = cid
        calls.append({"llm_call_id": cid, "run_id": SOURCE_RUN, "invocation_id": rec.get("invocation_id") or "",
                      "university_qid": qid, "provider": prov, "model": models.get(prov) if prov else None,
                      "purpose": kind if kind != "unknown" else None,
                      "cache_key": f[:-5] if f.endswith(".json") else f,
                      "raw_response_available": False, "called_at": rec.get("started"),
                      "note": LEGACY_CALL_NOTE if f == extract_file or kind != "extract" else
                      LEGACY_CALL_NOTE + " An empty extraction answer; another provider answered.",
                      "legacy": True, "source_file": gs(PREFIX + "cache/" + f, bucket)})

    if rec.get("status") != "ok":
        return calls, [], f"{qid}: attempt failed ({(rec.get('error') or '')[:120]})"
    if extract_file is None:
        return calls, [], f"{qid}: {len(answered)} non-empty extraction answers, expected 1"

    units = files[extract_file]
    report = rec.get("report") or {}
    csv_rows = list(csv.DictReader(io.StringIO(csv_text))) if csv_text else []
    unresolved_names = [u.get("name") for u in report.get("unresolved_candidates", [])]
    csv_name = PREFIX + f"missing_divisions_{qid}.csv"

    # Pair CSV rows and unresolved names with units by exact name, one unit per row.
    free = list(range(len(units)))
    status: Dict[int, Tuple[str, Optional[str]]] = {}
    for row in csv_rows:
        idx = next((i for i in free if units[i].get("name") == row["name"]), None)
        if idx is None:
            return calls, [], f"{qid}: CSV row '{row['name']}' is not in the extraction answer"
        free.remove(idx)
        status[idx] = (row["status"], row.get("qid") or None)
    for name in unresolved_names:
        idx = next((i for i in free if units[i].get("name") == name), None)
        if idx is None:
            return calls, [], f"{qid}: unresolved unit '{name}' is not in the extraction answer"
        free.remove(idx)
        status[idx] = ("unresolved", None)
    for i in free:
        status[i] = ("linked", None)

    got = Counter(s for s, _ in status.values())
    want = {"linked": report.get("exists_linked", 0), "orphan": report.get("exists_orphan", 0),
            "missing": report.get("missing", 0), "unresolved": report.get("unresolved", 0)}
    if len(units) != report.get("total_candidates") or any(got.get(k, 0) != v for k, v in want.items()):
        return calls, [], f"{qid}: counts {dict(got)} of {len(units)} units differ from the report {want}"

    cands = []
    for i, u in enumerate(units):
        st, matched = status[i]
        cands.append({
            "candidate_id": legacy_id(qid, extract_file, str(i)), "run_id": SOURCE_RUN,
            "invocation_id": rec.get("invocation_id") or "", "university_qid": qid, "parent_qid": qid,
            "name": u.get("name"), "unit_type": u.get("unit_type"), "status": st,
            "matched_qid": matched, "matched_label": None,
            "match_method": "none" if st in ("missing", "unresolved") else "legacy_unknown",
            "match_choices": None, "source_url": None, "website": u.get("website") or None,
            "provider": provider, "llm_call_ids": [call_id_of[extract_file]], "evidence_ids": [],
            "created_at": rec.get("started"), "legacy": True,
            "source_file": gs(csv_name if st in ("missing", "orphan") else PREFIX + "cache/" + extract_file, bucket)})
    return calls, cands, None


def add_matched_labels(cands: List[dict], label_of: Callable[[List[str]], Dict[str, Optional[str]]]) -> int:
    """Fill matched_label for every row that has a matched_qid. Returns how many."""
    qids = sorted({c["matched_qid"] for c in cands if c["matched_qid"]})
    names = label_of(qids) if qids else {}
    n = 0
    for c in cands:
        if c["matched_qid"]:
            c["matched_label"] = names.get(c["matched_qid"])
            n += c["matched_label"] is not None
    return n


def read_bucket(bucket, report=print) -> Tuple[Dict[str, dict], Dict[str, dict], Dict[str, object], Dict[str, str]]:
    """Everything the import needs from the bucket: records, models, cache files, CSVs."""
    recs = last_records(bucket.blob(PREFIX + "log.jsonl").download_as_text().splitlines())
    models = invocation_models(bucket.blob(PREFIX + "invocations.jsonl").download_as_text().splitlines())
    names = sorted({f for r in recs.values() for f in r.get("cache_files", [])})
    csvs = {b.name for b in bucket.client.list_blobs(bucket, prefix=PREFIX + "missing_divisions_")}
    report(f"reading {len(names)} cache files and {len(csvs)} CSV files from the bucket")

    def get_cache(f):
        return f, json.loads(bucket.blob(PREFIX + "cache/" + f).download_as_text())

    def get_csv(q):
        name = PREFIX + f"missing_divisions_{q}.csv"
        return q, bucket.blob(name).download_as_text() if name in csvs else None

    with ThreadPoolExecutor(16) as ex:
        cache = dict(ex.map(get_cache, names))
        csv_texts = dict(ex.map(get_csv, sorted(recs)))
    return recs, models, cache, csv_texts


def build_all(recs, models, cache, csv_texts, bucket_name="academiabot"):
    calls, cands, problems = [], [], []
    for qid in sorted(recs):
        rec = recs[qid]
        files = {f: cache[f] for f in rec.get("cache_files", []) if f in cache}
        c, k, problem = build_university(rec, files, csv_texts.get(qid),
                                         models.get(rec.get("invocation_id"), {}), bucket_name)
        calls += c
        cands += k
        if problem:
            problems.append(problem)
    return calls, cands, problems


ID_FIELD = {"llm_calls": "llm_call_id", "candidates": "candidate_id"}
LOAD_ORDER = ("llm_calls", "candidates")


def missing_rows(rows: List[dict], id_field: str, have: set) -> List[dict]:
    """The rows whose id is not in `have`, each id once."""
    seen = set(have)
    out = []
    for r in rows:
        if r[id_field] not in seen:
            seen.add(r[id_field])
            out.append(r)
    return out


def import_rows(rows: Dict[str, List[dict]], existing_ids: Callable[[str], set],
                load: Callable[[str, List[dict]], None]) -> Dict[str, int]:
    """Load each table's rows that are not there yet. Returns rows added per table.

    The ids are read from each table right before its load, so a retry after a partial
    failure (llm_calls loaded, candidates failed) adds only what is missing. A failed
    load raises, so the caller can record the run as failed."""
    added = {}
    for t in LOAD_ORDER:
        new = missing_rows(rows.get(t, []), ID_FIELD[t], existing_ids(t))
        if new:
            load(t, new)
        added[t] = len(new)
    return added


def legacy_ids(bq, table: str) -> set:
    q = (f"SELECT {ID_FIELD[table]} AS id FROM `{run_log.table_ref(table)}` "
         f"WHERE run_id = '{SOURCE_RUN}' AND legacy")
    return {r.id for r in bq.query(q).result()}


def load_table(bq, table: str, rows: List[dict]) -> None:
    from google.cloud import bigquery
    job = bq.load_table_from_json(
        rows, run_log.table_ref(table),
        job_config=bigquery.LoadJobConfig(schema=run_log.bigquery_schema(table), write_disposition="WRITE_APPEND"))
    job.result()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Import run us-tier1 into llm_calls and candidates, as legacy rows")
    p.add_argument("--write", action="store_true", help="Load into BigQuery. Without it, a dry run")
    args = p.parse_args(argv)
    from google.cloud import bigquery, storage
    from wikidata_discover import wikidata_api
    from wikidata_discover.batch import operator_identity, source_identity
    bucket = storage.Client(project=config.GCP_PROJECT).bucket(config.RUNLOG_BUCKET)
    bq = bigquery.Client(project=config.GCP_PROJECT)
    run_id = run_log.new_run_id("import-legacy")
    run_dir = config.RESULTS_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = run_log.start(run_id, command="import-legacy", args={"argv": ["import-legacy"] + list(argv or sys.argv[1:])},
                        who=operator_identity(), git_commit=source_identity(run_dir), bucket=bucket,
                        bq_client=bq, run_config={"source_run": SOURCE_RUN, "write": args.write})
    outcome = "failed"
    summary: dict = {}
    try:
        recs, models, cache, csv_texts = read_bucket(bucket)
        calls, cands, problems = build_all(recs, models, cache, csv_texts, config.RUNLOG_BUCKET)
        # Labels are read from Wikidata only for a real import, so a dry run sends no
        # request to Wikidata (a snapshot run may be using the same address). A retry
        # reads them only for candidates that are not loaded yet.
        labelled = 0
        if args.write:
            have = legacy_ids(bq, "candidates")
            labelled = add_matched_labels([c for c in cands if c["candidate_id"] not in have], wikidata_api.labels)
        st = Counter(c["status"] for c in cands)
        summary = {"universities": len(recs), "imported_universities": len({c["university_qid"] for c in cands}),
                   "llm_calls": len(calls), "candidates": len(cands), "by_status": dict(st),
                   "orphans_with_label": labelled, "linked_without_match": st.get("linked", 0),
                   "problems": problems, "write": args.write}
        text = "\n".join(json.dumps(run_log.clean_row("candidates", c)) for c in cands)
        (run_dir / "candidates.jsonl").write_text(text + "\n")
        (run_dir / "llm_calls.jsonl").write_text(
            "\n".join(json.dumps(run_log.clean_row("llm_calls", c)) for c in calls) + "\n")
        log.save_text("import_summary.json", json.dumps(summary, indent=1))
        if args.write:
            # Straight to BigQuery: these rows keep run_id us-tier1 and the original
            # invocation ids, so they do not go through this invocation's run log. Each
            # table skips ids it already has, so a retry after a partial failure is safe.
            rows = {"llm_calls": [run_log.clean_row("llm_calls", c) for c in calls],
                    "candidates": [run_log.clean_row("candidates", c) for c in cands]}
            added = import_rows(rows, lambda t: legacy_ids(bq, t), lambda t, r: load_table(bq, t, r))
            summary["added"] = added
            summary["already_there"] = {t: len(rows[t]) - added[t] for t in LOAD_ORDER}
            log.counts["llm_calls"], log.counts["candidates"] = added["llm_calls"], added["candidates"]
        outcome = "ok"
    except Exception as e:  # noqa: BLE001 - recorded in the runs row, then raised
        summary["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        print(f"The import stopped: {summary['error']}")
        print("Run --write again. It adds only the rows that are not in BigQuery yet.")
        raise
    finally:
        status = log.finish(outcome, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "problems"}, indent=1))
    print(f"{len(summary.get('problems', []))} universities not imported:")
    for pr in summary.get("problems", []):
        print("  " + pr)
    print(f"Rows also saved to {run_dir}")
    print(run_log.describe(status))
    return 0 if outcome == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
