"""Cloud Function (gen 2, HTTP) that advances a collection run by one time slice.

Each invocation: read the university list and the run log from the bucket, pick the
next universities not yet done, run discovery on them until the time budget is spent,
upload everything, and return a JSON summary. Cloud Scheduler calls it on a schedule
(for example hourly) until the run log shows every university done.

Request JSON (all optional):
  run_id            default "cloud-<yyyy-mm-dd>"; keep it fixed for a multi-day run
  list_object       default "universities_us.json" (bucket object: [[qid, label], ...])
  max_universities  default 60, cap per invocation independent of the time budget
  time_budget_s     default 3000 (50 minutes; the function timeout is 60)
  qids              explicit list, overrides list_object

Deploy with deploy/deploy_collect_function.sh. Keys come from Secret Manager at runtime.
"""
import json
import logging
import os
import time

import functions_framework
from flask import jsonify

from wikidata_discover.batch import BUCKET, PROJECT, load_keys_from_secret_manager, parse_done, run_batch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def pick_qids(list_rows, done: set, limit: int):
    """First `limit` QIDs from the bucket list that are not done, in list order,
    de-duplicated. list_rows is [[qid, label], ...] or [qid, ...]."""
    seen, out = set(), []
    for row in list_rows:
        qid = row[0] if isinstance(row, (list, tuple)) else row
        if qid in seen or qid in done:
            continue
        seen.add(qid)
        out.append(qid)
        if len(out) >= limit:
            break
    return out


@functions_framework.http
def collect(request):
    body = request.get_json(silent=True) or {}
    run_id = body.get("run_id") or f"cloud-{time.strftime('%Y-%m-%d', time.gmtime())}"
    list_object = body.get("list_object", "universities_us.json")
    limit = int(body.get("max_universities", 60))
    budget = float(body.get("time_budget_s", 3000))

    load_keys_from_secret_manager()
    os.environ.setdefault("WD_BOT_USERAGENT", "AcademiaBot/1.0 (ipeirotis@gmail.com)")

    from google.cloud import storage
    bucket = storage.Client(project=PROJECT).bucket(BUCKET)

    if body.get("qids"):
        qids = list(body["qids"])
    else:
        rows = json.loads(bucket.blob(list_object).download_as_text())
        log_blob = bucket.blob(f"runs/{run_id}/log.jsonl")
        done = parse_done(log_blob.download_as_text()) if log_blob.exists() else set()
        qids = pick_qids(rows, done, limit)
        if not qids:
            return jsonify({"run_id": run_id, "message": "nothing left to do", "done": len(done)})

    summary = run_batch(run_id, qids, bucket, time_budget_s=budget, report=logger.info)
    return jsonify(summary), (200 if summary["failed"] == 0 else 207)
