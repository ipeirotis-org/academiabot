"""Cloud Function (gen 2, HTTP) that advances a collection run by one time slice.

Each invocation: read the university list and the run log from the bucket, pick the
next universities not yet done, run discovery on them until the time budget is spent,
upload everything, and return a JSON summary. Cloud Scheduler calls it on a schedule
(every 30 minutes) until the run log shows every university done.

Request JSON (all optional):
  run_id            default "cloud-<yyyy-mm-dd>"; keep it fixed for a multi-day run
  list_object       default "universities_us.json" (bucket object: [[qid, label], ...],
                    [qid, ...], or the SPARQL binding rows that `harvest` writes)
  max_universities  default 60, cap per invocation independent of the time budget
  time_budget_s     default 1500 (25 minutes). Cloud Scheduler cancels an HTTP call
                    after 30 minutes at most, so the slice must return before that
  reserve_s         default 420: no university starts unless this much of the budget
                    (or the longest university so far, if more) is still left
  qids              explicit list, overrides list_object; still de-duplicated and
                    capped at max_universities. An empty list means do nothing
                    (the invocation is still recorded)

Deploy with deploy/deploy_collect_function.sh. Keys come from Secret Manager at runtime.
"""
import json
import logging
import os
import time

import functions_framework
from flask import jsonify

from wikidata_discover.batch import BUCKET, PROJECT, ensure_user_agent, load_keys_from_secret_manager, parse_done, run_batch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def row_qid(row) -> str:
    """QID of one university-list row. Accepts the three shapes we have used:
    "Q1", ["Q1", "label"], and the SPARQL binding dict that `harvest` writes
    ({"university": {"value": "http://www.wikidata.org/entity/Q1"}, ...})."""
    if isinstance(row, dict):
        value = (row.get("university") or row.get("univ") or {}).get("value", "")
    elif isinstance(row, (list, tuple)):
        value = row[0]
    else:
        value = row
    return str(value).rsplit("/", 1)[-1]


def pick_qids(list_rows, done: set, limit: int):
    """First `limit` QIDs from the bucket list that are not done, in list order,
    de-duplicated. Rows may be any shape row_qid() accepts."""
    seen, out = set(), []
    if limit <= 0:
        return out
    for row in list_rows:
        qid = row_qid(row)
        if not qid or qid in seen or qid in done:
            continue
        seen.add(qid)
        out.append(qid)
        if len(out) >= limit:
            break
    return out


class _UnreachableBucket:
    """Stands in for the bucket when the storage client could not be built, so
    run_batch can still record the failed invocation locally."""
    def __init__(self, reason: str):
        self.reason = reason
    def _fail(self, *a, **k):
        raise RuntimeError(self.reason)
    def blob(self, name):
        return self
    exists = download_as_text = upload_from_filename = delete = list_blobs = _fail


@functions_framework.http
def collect(request):
    body = request.get_json(silent=True) or {}
    run_id = body.get("run_id") or f"cloud-{time.strftime('%Y-%m-%d', time.gmtime())}"
    list_object = body.get("list_object", "universities_us.json")
    limit = int(body.get("max_universities", 60))
    budget = float(body.get("time_budget_s", 1500))
    reserve = float(body.get("reserve_s", 420))

    # Who called: the scheduler job (its header names the job) or a person by hand
    # (the authenticated caller, if the platform passes it; otherwise "manual").
    headers = getattr(request, "headers", {}) or {}
    job = headers.get("X-CloudScheduler-JobName")
    os.environ["ACADEMIABOT_OPERATOR"] = (f"cloud-scheduler:{job}" if job else
                                          f"manual:{headers.get('X-Goog-Authenticated-User-Email', 'unknown caller')}")

    args = {"request": body, "run_id": run_id, "list_object": None, "max_universities": limit,
            "time_budget_s": budget, "reserve_s": reserve, "explicit_qids": "qids" in body}
    # Every failure from here on is recorded through run_batch (local records, and on
    # a cold instance a refusal to run blind) instead of escaping as an unlogged 500.
    qids, bucket = [], None
    try:
        load_keys_from_secret_manager()
        ensure_user_agent()
        from google.cloud import storage
        bucket = storage.Client(project=PROJECT).bucket(BUCKET)
    except Exception as e:  # noqa: BLE001
        args["preflight_error"] = f"init: {type(e).__name__}: {str(e)[:200]}"
        logger.error("initialisation failed: %s", args["preflight_error"])
        bucket = _UnreachableBucket(args["preflight_error"])
    try:
        if "preflight_error" in args:
            raise RuntimeError(args["preflight_error"])
        log_blob = bucket.blob(f"runs/{run_id}/log.jsonl")
        done = parse_done(log_blob.download_as_text()) if log_blob.exists() else set()
        args["done_before"] = len(done)
        if "qids" in body:
            # An explicit list, even an empty one, never falls back to the bucket list.
            # Done QIDs are dropped before the cap, so a long list advances across calls.
            qids = pick_qids(body["qids"] or [], done, limit)
        else:
            args["list_object"] = list_object
            rows = json.loads(bucket.blob(list_object).download_as_text())
            qids = pick_qids(rows, done, limit)
            args["list_done"] = len(done)
    except Exception as e:  # noqa: BLE001
        if "preflight_error" not in args:
            args["preflight_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            logger.error("preflight failed, nothing selected: %s", args["preflight_error"])

    # An empty list still goes through run_batch so the invocation is recorded; a
    # preflight failure is passed in so the recorded outcome is failed, not ok.
    summary = run_batch(run_id, qids, bucket, time_budget_s=budget, reserve_s=reserve,
                        report=logger.info, invocation_args=args, fail_reason=args.get("preflight_error"))
    if "preflight_error" in args:
        summary["message"] = "preflight failed: " + args["preflight_error"]
    elif not qids:
        summary["message"] = "nothing left to do" if "list_done" in args else "no QIDs requested"
    return jsonify(summary), (200 if summary["failed"] == 0 else 207)
