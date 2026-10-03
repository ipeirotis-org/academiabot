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
import base64
import json
import logging
import math
import os
import time

import functions_framework
from flask import jsonify

from wikidata_discover import config
from wikidata_discover.batch import (BUCKET, PROJECT, UnreachableBucket, bucket_call_kwargs, ensure_user_agent,
                                     is_qid, load_keys_from_secret_manager, parse_done, run_batch, validate_run_id)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# The function's own timeout (deploy script: --timeout=1800s). Wikidata retries stop
# before this so the attempt's records are always written.
FUNCTION_TIMEOUT_S = float(os.getenv("FUNCTION_TIMEOUT_S", "1800"))


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
        if not is_qid(qid):
            if qid:
                logger.warning("university list row %r is not a QID; skipped", row)
            continue
        if qid in seen or qid in done:
            continue
        seen.add(qid)
        out.append(qid)
        if len(out) >= limit:
            break
    return out


# The account Cloud Scheduler calls with (deploy script: --oidc-service-account-email).
SCHEDULER_SA = os.getenv("SCHEDULER_SA", "claude-agent@wikidata-academia.iam.gserviceaccount.com")


def token_email(headers) -> str:
    """Email claim of the bearer ID token, or "". Cloud Run IAM verified the token
    before delivering the request, so the claim can be read without a second
    signature check. When both headers are present Cloud Run checks only
    X-Serverless-Authorization, so that one is read first; Authorization is used
    only when it is the sole header. Nothing else is trusted for identity."""
    auth = headers.get("X-Serverless-Authorization") or headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth.count(".") == 2:
        try:
            payload = auth.split(" ", 1)[1].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return str(claims.get("email") or "")
        except Exception:  # noqa: BLE001 - an unreadable token is just an unknown caller
            pass
    return ""


def caller_identity(headers) -> str:
    """Who made this request: "cloud-scheduler:<job>" only when the verified token
    belongs to the scheduler's account and the scheduler's job header is present;
    otherwise "manual:<email>" from the verified token, or "manual:unknown caller"."""
    email = token_email(headers)
    job = headers.get("X-CloudScheduler-JobName")
    if job and email == SCHEDULER_SA:
        return f"cloud-scheduler:{job}"
    return f"manual:{email}" if email else "manual:unknown caller"


def parse_request(body) -> dict:
    """Validate and normalize the request body. Raises ValueError on bad input."""
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValueError(f"request body must be a JSON object, got {type(body).__name__}")
    p = {"run_id": validate_run_id(body.get("run_id") or f"cloud-{time.strftime('%Y-%m-%d', time.gmtime())}"),
         "list_object": str(body.get("list_object", "universities_us.json"))}
    if p["list_object"].startswith("/") or ".." in p["list_object"].split("/"):
        raise ValueError(f"invalid list_object {p['list_object']!r}")
    for key, default, cast in (("max_universities", 60, int), ("time_budget_s", 1500.0, float), ("reserve_s", 420.0, float)):
        try:
            value = cast(body.get(key, default))
        except (TypeError, ValueError, OverflowError) as e:
            raise ValueError(f"{key} must be a number, got {body.get(key)!r}") from e
        # "nan" and "inf" pass float(); a NaN reserve makes every time check false and
        # the loop would start universities after the deadline, failing each of them.
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be a finite number >= 0, got {body.get(key)!r}")
        p[key] = value
    if "qids" in body:
        if body["qids"] is not None and not isinstance(body["qids"], list):
            raise ValueError("qids must be a list")
        bad = [q for q in (body["qids"] or []) if not is_qid(q)]
        if bad:
            raise ValueError(f"qids must be Wikidata item ids like Q49210, got {bad[:3]!r}")
        p["qids"] = list(body["qids"] or [])
    return p


@functions_framework.http
def collect(request):
    entered = time.time()   # the platform's timeout counts from here, not from run_batch
    # The kill time is known now, so the preflight bucket reads below are bounded by
    # it too (run_batch sets the same value again from hard_deadline_s).
    config.HARD_DEADLINE = entered + FUNCTION_TIMEOUT_S
    try:
        return _collect(request, entered)
    finally:
        config.DEADLINE = config.HARD_DEADLINE = None


def _collect(request, entered: float):
    headers = getattr(request, "headers", {}) or {}
    os.environ["ACADEMIABOT_OPERATOR"] = caller_identity(headers)
    raw = request.get_json(silent=True)
    args = {"request": raw if isinstance(raw, (dict, list)) else None}

    # Every failure from here on is recorded through run_batch (local records, and on
    # a cold instance a refusal to run blind) instead of escaping as an unlogged 500.
    qids, bucket = [], None
    try:
        body = getattr(request, "data", b"") or b""
        if raw is None and body.strip():
            # A body that is not JSON must not pass for "no body" and start the
            # default run on the default list: that spends LLM credit by accident.
            raise ValueError("body is not valid JSON")
        p = parse_request(raw)
    except ValueError as e:
        p = parse_request({})
        args["preflight_error"] = f"bad request: {e}"
    run_id, list_object, limit = p["run_id"], p["list_object"], p["max_universities"]
    budget, reserve = p["time_budget_s"], p["reserve_s"]
    args.update({"run_id": run_id, "list_object": None, "max_universities": limit,
                 "time_budget_s": budget, "reserve_s": reserve, "explicit_qids": "qids" in p})
    # The bucket is built whatever else failed: a bad request or a missing key is
    # still recorded in the run history, not only on an instance that may vanish.
    storage_error = None
    try:
        from google.cloud import storage
        bucket = storage.Client(project=PROJECT).bucket(BUCKET)
    except Exception as e:  # noqa: BLE001
        storage_error = f"storage: {type(e).__name__}: {str(e)[:200]}"
    if "preflight_error" not in args:
        try:
            load_keys_from_secret_manager()
            ensure_user_agent()
        except Exception as e:  # noqa: BLE001
            args["preflight_error"] = f"init: {type(e).__name__}: {str(e)[:200]}"
    if bucket is None:
        args.setdefault("preflight_error", storage_error)
        bucket = UnreachableBucket(storage_error)
    try:
        if "preflight_error" in args:
            raise RuntimeError(args["preflight_error"])
        log_blob = bucket.blob(f"runs/{run_id}/log.jsonl")
        done = (parse_done(log_blob.download_as_text(**bucket_call_kwargs()))
                if log_blob.exists(**bucket_call_kwargs()) else set())
        args["done_before"] = len(done)
        if "qids" in p:
            # An explicit list, even an empty one, never falls back to the bucket list.
            # Done QIDs are dropped before the cap, so a long list advances across calls.
            qids = pick_qids(p["qids"], done, limit)
        else:
            args["list_object"] = list_object
            rows = json.loads(bucket.blob(list_object).download_as_text(**bucket_call_kwargs()))
            qids = pick_qids(rows, done, limit)
            args["list_done"] = len(done)
    except Exception as e:  # noqa: BLE001
        if "preflight_error" not in args:
            args["preflight_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    if "preflight_error" in args:
        logger.error("preflight failed, nothing selected: %s", args["preflight_error"])

    # An empty list still goes through run_batch so the invocation is recorded; a
    # preflight failure is passed in so the recorded outcome is failed, not ok.
    summary = run_batch(run_id, qids, bucket, time_budget_s=budget, reserve_s=reserve,
                        hard_deadline_s=FUNCTION_TIMEOUT_S - (time.time() - entered), report=logger.info,
                        invocation_args=args, fail_reason=args.get("preflight_error"))
    if "preflight_error" in args:
        summary["message"] = "preflight failed: " + args["preflight_error"]
    elif not qids:
        summary["message"] = "nothing left to do" if "list_done" in args else "no QIDs requested"
        if summary.get("needs_review"):
            summary["message"] += f"; {summary['needs_review']} universities were given up on and need a person"
    return jsonify(summary), (200 if summary["failed"] == 0 else 207)
