"""Batch discovery over many universities, resumable, with every artifact uploaded.

Used by scripts/batch_collect.py (run from a laptop or this repo) and by
cloud/collect_function.py (run as a Cloud Function on a schedule). State lives in the
bucket under runs/<run_id>/: log.jsonl has one record per university attempt, and a
university counts as done only when discovery succeeded and its files were uploaded.
"""
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Iterable, Optional

from wikidata_discover.config import RESULTS_DIR

logger = logging.getLogger(__name__)

BUCKET = "academiabot"
PROJECT = "wikidata-academia"
SECRETS = [("OPENAI_API_KEY", "openai-api-key"), ("ANTHROPIC_API_KEY", "anthropic-api-key"), ("GOOGLE_API_KEY", "gemini-api-key")]


def artifact_paths(results_dir: Path, run_dir: Path, qid: str, since: float, extra=()):
    """Files to upload for one QID: everything in the run folder, the QID's own CSV,
    QuickStatements file and report if modified at or after `since`, plus `extra`
    (the cache files this QID's discovery read or wrote). Exact names only, so stale
    outputs from earlier runs for the same QID are never attributed to this run."""
    exact = [
        results_dir / f"missing_divisions_{qid}.csv",
        results_dir / f"quickstatements_{qid}.qs",
        results_dir / "reports" / f"{qid}_report.json",
    ]
    fresh = [p for p in exact if p.is_file() and p.stat().st_mtime >= since]
    run_files = [p for p in run_dir.rglob("*") if p.is_file()]
    extra_files = [Path(p) for p in extra if Path(p).is_file()]
    return run_files + fresh + extra_files


def parse_done(log_text: str) -> set:
    """QIDs whose discovery succeeded and whose files reached the bucket."""
    done = set()
    for line in log_text.splitlines():
        try:
            rec = json.loads(line)
            if rec.get("status") == "ok" and rec.get("uploaded"):
                done.add(rec["qid"])
        except Exception:
            pass
    return done


def load_done(log_path: Path) -> set:
    return parse_done(log_path.read_text()) if log_path.exists() else set()


def load_keys_from_secret_manager():
    """Fill in any missing LLM key from Secret Manager, into this process only."""
    from google.cloud import secretmanager
    sm = secretmanager.SecretManagerServiceClient()
    for env, secret in SECRETS:
        if not os.getenv(env):
            name = f"projects/{PROJECT}/secrets/{secret}/versions/latest"
            os.environ[env] = sm.access_secret_version(request={"name": name}).payload.data.decode().strip()


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(RESULTS_DIR.parents[1]), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out or os.getenv("GIT_COMMIT", "unknown")
    except Exception:
        return os.getenv("GIT_COMMIT", "unknown")


def run_batch(run_id: str, qids: Iterable[str], bucket, time_budget_s: Optional[float] = None,
              results_dir: Path = RESULTS_DIR, report: Callable[[str], None] = print) -> dict:
    """Run discovery for each QID not already done, uploading as it goes.

    bucket: a google.cloud.storage Bucket. time_budget_s: stop starting new QIDs once this
    many seconds have passed (for a Cloud Function with a hard timeout). Returns a summary
    dict with counts; 'failed' > 0 means something needs attention.
    """
    from wikidata_discover import config, llm_helpers
    from wikidata_discover.discovery import Discovery

    qids = list(qids)
    started = time.time()
    run_dir = results_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "log.jsonl"

    def upload(paths) -> int:
        n = 0
        for p in paths:
            bucket.blob(f"runs/{run_id}/{p.relative_to(results_dir)}").upload_from_filename(str(p)); n += 1
        return n

    # Resume from the bucket's copy of the log when the local one is missing or shorter
    # (a fresh Cloud Function instance has no local state).
    remote_log = bucket.blob(f"runs/{run_id}/log.jsonl")
    if remote_log.exists():
        remote_text = remote_log.download_as_text()
        local_text = log_path.read_text() if log_path.exists() else ""
        if len(remote_text) > len(local_text):
            log_path.write_text(remote_text)
    resumed = (run_dir / "run.json").exists() or bucket.blob(f"runs/{run_id}/run.json").exists()
    done = load_done(log_path)

    invocation = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git_commit": git_commit(),
                  "command": sys.argv, "host": os.getenv("K_SERVICE", "local"),
                  "models": {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL, "gemini": config.GEMINI_MODEL},
                  "user_agent": config.USER_AGENT, "qids": qids, "resumed": resumed, "time_budget_s": time_budget_s}
    if not resumed:
        (run_dir / "run.json").write_text(json.dumps({"run_id": run_id, **invocation}, indent=2))
    with (run_dir / "invocations.jsonl").open("a") as f:
        f.write(json.dumps(invocation) + "\n")

    summary = {"run_id": run_id, "requested": len(qids), "skipped_done": 0, "processed": 0, "ok": 0,
               "failed": 0, "stopped_for_time": False}
    for i, qid in enumerate(qids, 1):
        if qid in done:
            summary["skipped_done"] += 1
            continue
        if time_budget_s is not None and time.time() - started > time_budget_s:
            summary["stopped_for_time"] = True
            report(f"time budget reached after {summary['processed']} universities; stopping")
            break
        t = time.time()
        rec = {"qid": qid, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": invocation["host"]}
        llm_helpers.cache_paths_touched.clear()
        try:
            d = Discovery(qid)
            d.discover_missing()
            rep = results_dir / "reports" / f"{qid}_report.json"
            rpt = json.loads(rep.read_text()) if rep.exists() else {}
            rec.update({"status": "ok", "label": getattr(d, "university_label", None), "report": rpt,
                        "missing_rows": rpt.get("missing", 0), "orphan_rows": rpt.get("exists_orphan", 0),
                        "unresolved_rows": rpt.get("unresolved", 0)})
            summary["ok"] += 1
        except Exception as e:  # noqa: BLE001 - one university must not stop the batch
            rec.update({"status": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}",
                        "trace": traceback.format_exc()[-1500:]})
            summary["failed"] += 1
        rec["seconds"] = round(time.time() - t, 1)
        rec["cache_files"] = sorted(p.name for p in llm_helpers.cache_paths_touched)
        try:
            n = upload(artifact_paths(results_dir, run_dir, qid, since=t, extra=list(llm_helpers.cache_paths_touched)))
            rec["uploaded"] = True
            note = f"uploaded {n} files to gs://{BUCKET}/runs/{run_id}/"
        except Exception as e:  # noqa: BLE001
            rec["uploaded"] = False
            note = f"upload failed: {type(e).__name__}: {str(e)[:120]} (QID will be retried on resume)"
            summary["failed"] += 1
        with log_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        try:
            upload([p for p in run_dir.rglob("*") if p.is_file()])
        except Exception as e:  # noqa: BLE001
            note += f"; run log upload failed: {type(e).__name__}"
            summary["failed"] += 1
        summary["processed"] += 1
        report(f"[{i}/{len(qids)}] {qid} {rec['status']} {rec.get('label', '')} {rec['seconds']}s")
        report(f"   {note}")
    summary["seconds"] = round(time.time() - started, 1)
    return summary
