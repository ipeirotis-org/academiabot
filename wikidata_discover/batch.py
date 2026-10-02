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
    """QIDs whose discovery succeeded and whose files reached the bucket.

    The last record for a QID wins, so a later record with uploaded=false (written
    when the run log itself failed to upload) puts the QID back in the queue."""
    last = {}
    for line in log_text.splitlines():
        try:
            rec = json.loads(line)
            last[rec["qid"]] = rec.get("status") == "ok" and bool(rec.get("uploaded"))
        except Exception:
            pass
    return {qid for qid, ok in last.items() if ok}


def load_done(log_path: Path) -> set:
    return parse_done(log_path.read_text()) if log_path.exists() else set()


def load_keys_from_secret_manager(client=None) -> dict:
    """Fill in any missing LLM key from Secret Manager, into this process only.

    Sets both the environment variable and the matching config attribute, because
    config has usually been imported (and read os.environ) before this runs. A secret
    that is missing or inaccessible is logged and skipped: one working provider is a
    supported setup. Raises RuntimeError only when no key is available at all.
    Returns {env_name: "env" | "secret_manager" | "missing"}."""
    from wikidata_discover import config
    if client is None:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
    sources = {}
    for env, secret in SECRETS:
        value = os.getenv(env)
        sources[env] = "env"
        if not value:
            name = f"projects/{PROJECT}/secrets/{secret}/versions/latest"
            try:
                value = client.access_secret_version(request={"name": name}).payload.data.decode().strip()
                sources[env] = "secret_manager"
            except Exception as e:  # noqa: BLE001 - one missing optional key must not stop a run
                logger.warning("Secret %s not available (%s: %s); provider %s disabled", secret, type(e).__name__, str(e)[:120], env)
                value, sources[env] = None, "missing"
            if value:
                os.environ[env] = value
        setattr(config, env, value or None)
    if not any(getattr(config, env) for env, _ in SECRETS):
        raise RuntimeError("No LLM API key available from the environment or Secret Manager: " + ", ".join(s for _, s in SECRETS))
    return sources


def ensure_user_agent(default: str = "AcademiaBot/1.0 (ipeirotis@gmail.com)") -> str:
    """Make sure Wikidata requests identify us. WD_BOT_USERAGENT wins if set; otherwise
    `default` is applied to config as well as the environment, because config has
    usually been imported before this runs."""
    from wikidata_discover import config
    config.set_user_agent(os.getenv("WD_BOT_USERAGENT") or default)
    return config.USER_AGENT


def restore_caches(bucket, run_id: str, log_path: Path, results_dir: Path, done: set) -> int:
    """Bring back, from the bucket, the LLM cache files that earlier attempts at still
    pending QIDs read or wrote, so a retry on a fresh instance reuses the same LLM
    answers instead of paying for (and possibly getting) new ones. Returns the count."""
    if not log_path.exists():
        return 0
    wanted = set()
    for line in log_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("qid") not in done:
            wanted.update(rec.get("cache_files") or [])
    cache_dir = results_dir / "cache"
    restored = 0
    for name in sorted(wanted):
        local = cache_dir / name
        if local.exists():
            continue
        blob = bucket.blob(f"runs/{run_id}/cache/{name}")
        if blob.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            local.write_text(blob.download_as_text())
            restored += 1
    return restored


def sync_from_bucket(bucket, run_id: str, run_dir: Path, name: str) -> None:
    """Replace the local copy of an append-only run file with the bucket's when the
    bucket's is longer (a fresh Cloud Function instance has no local state)."""
    blob = bucket.blob(f"runs/{run_id}/{name}")
    if blob.exists():
        remote_text = blob.download_as_text()
        local = run_dir / name
        local_text = local.read_text() if local.exists() else ""
        if len(remote_text) > len(local_text):
            local.write_text(remote_text)


def object_name(run_id: str, path: Path, results_dir: Path, run_dir: Path) -> str:
    """Bucket object for a local file: run files land directly under runs/<run_id>/,
    other outputs keep their path relative to the results folder."""
    base = run_dir if run_dir in path.parents else results_dir
    return f"runs/{run_id}/{path.relative_to(base).as_posix()}"


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(RESULTS_DIR.parents[1]), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out or os.getenv("GIT_COMMIT", "unknown")
    except Exception:
        return os.getenv("GIT_COMMIT", "unknown")


def run_batch(run_id: str, qids: Iterable[str], bucket, time_budget_s: Optional[float] = None,
              results_dir: Path = RESULTS_DIR, report: Callable[[str], None] = print,
              invocation_args: Optional[dict] = None, reserve_s: float = 0) -> dict:
    """Run discovery for each QID not already done, uploading as it goes.

    bucket: a google.cloud.storage Bucket. time_budget_s: stop starting new QIDs once
    the time used plus a reserve for the next one would exceed this (for a Cloud
    Function with a hard timeout). The reserve is reserve_s or the longest university
    so far in this invocation, whichever is larger. invocation_args: what selected
    this run (the CLI arguments, or the HTTP request body and the values resolved from
    it), recorded in run.json and invocations.jsonl so the run can be repeated. Returns
    a summary dict with counts; 'failed' > 0 means something needs attention.
    """
    from wikidata_discover import config, llm_helpers
    from wikidata_discover.discovery import Discovery

    qids = list(dict.fromkeys(qids))  # de-duplicate, keeping order
    started = time.time()
    run_dir = results_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "log.jsonl"

    def upload(paths) -> int:
        n = 0
        for p in paths:
            bucket.blob(object_name(run_id, p, results_dir, run_dir)).upload_from_filename(str(p)); n += 1
        return n

    for name in ("log.jsonl", "invocations.jsonl"):
        sync_from_bucket(bucket, run_id, run_dir, name)
    resumed = (run_dir / "run.json").exists() or bucket.blob(f"runs/{run_id}/run.json").exists()
    done = load_done(log_path)
    restored = restore_caches(bucket, run_id, log_path, results_dir, done)
    if restored:
        report(f"restored {restored} cache files from the bucket for pending universities")

    invocation = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git_commit": git_commit(),
                  "args": invocation_args if invocation_args is not None else {"argv": sys.argv},
                  "host": os.getenv("K_SERVICE", "local"),
                  "models": {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL, "gemini": config.GEMINI_MODEL},
                  "user_agent": config.USER_AGENT, "qids": qids, "resumed": resumed,
                  "time_budget_s": time_budget_s, "reserve_s": reserve_s}
    if not resumed:
        (run_dir / "run.json").write_text(json.dumps({"run_id": run_id, **invocation}, indent=2))
    with (run_dir / "invocations.jsonl").open("a") as f:
        f.write(json.dumps(invocation) + "\n")

    summary = {"run_id": run_id, "requested": len(qids), "skipped_done": 0, "processed": 0, "ok": 0,
               "failed": 0, "stopped_for_time": False}
    # The invocation is part of the record even when no university gets processed.
    try:
        upload([p for p in run_dir.rglob("*") if p.is_file()])
    except Exception as e:  # noqa: BLE001
        report(f"run metadata upload failed: {type(e).__name__}: {str(e)[:120]}")
        summary["failed"] += 1

    longest = 0.0
    for i, qid in enumerate(qids, 1):
        if qid in done:
            summary["skipped_done"] += 1
            continue
        if time_budget_s is not None and time.time() - started + max(reserve_s, longest) > time_budget_s:
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
        longest = max(longest, rec["seconds"])
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
            # The bucket may not have this record, so a resume from the bucket would
            # retry the QID anyway. Make the local log agree: a second record with
            # uploaded=false puts the QID back in the queue here too.
            note += f"; run log upload failed: {type(e).__name__} (QID will be retried on resume)"
            if rec["uploaded"]:
                with log_path.open("a") as f:
                    f.write(json.dumps({"qid": qid, "status": "ok", "uploaded": False,
                                        "note": f"run log upload failed: {type(e).__name__}"}) + "\n")
                rec["uploaded"] = False
                summary["failed"] += 1
        if rec["status"] == "ok" and rec["uploaded"]:
            done.add(qid)
        summary["processed"] += 1
        report(f"[{i}/{len(qids)}] {qid} {rec['status']} {rec.get('label', '')} {rec['seconds']}s")
        report(f"   {note}")
    summary["seconds"] = round(time.time() - started, 1)
    return summary
