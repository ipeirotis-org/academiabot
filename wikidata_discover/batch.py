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


MAX_ATTEMPTS = 3  # attempts at a university before it is left for a person to look at


def parse_done(log_text: str, max_attempts: int = MAX_ATTEMPTS) -> set:
    """QIDs that need no further attempt: discovery succeeded, the files reached the
    bucket, and every candidate was checked against Wikidata.

    The last record for a QID wins, so a later record with uploaded=false (written
    when the run log itself failed to upload) puts the QID back in the queue. A
    university with unresolved candidates (Wikidata could not be searched) or a
    failed attempt is retried, up to max_attempts attempts in total; after that it is
    left alone and its log records are for a person to read (a QID that fails three
    times is usually not a university, or the LLM found nothing for it)."""
    last, attempts = {}, {}
    for line in log_text.splitlines():
        try:
            rec = json.loads(line)
            qid = rec["qid"]
            if "started" in rec:
                attempts[qid] = attempts.get(qid, 0) + 1
            complete = rec.get("status") == "ok" and bool(rec.get("uploaded"))
            unresolved = rec.get("unresolved_rows") or 0
            last[qid] = (complete and unresolved == 0) or attempts.get(qid, 0) >= max_attempts
        except Exception:
            pass
    return {qid for qid, ok in last.items() if ok}


def load_done(log_path: Path) -> set:
    return parse_done(log_path.read_text()) if log_path.exists() else set()


_injected: dict = {}  # env var -> value we copied in from Secret Manager (not a deployment setting)


def load_keys_from_secret_manager(client=None) -> dict:
    """Fill in any missing LLM key from Secret Manager, into this process only.

    Sets both the environment variable and the matching config attribute, because
    config has usually been imported (and read os.environ) before this runs. A value
    this function itself copied in earlier is not treated as a deployment setting:
    it is fetched again on every call, so a warm instance picks up a rotated key, and
    the cached provider clients are reset when a key changes. A secret that is
    missing or inaccessible is logged and skipped: one working provider is a supported
    setup. Raises RuntimeError only when no key is available at all.
    Returns {env_name: "env" | "secret_manager" | "missing"}."""
    from wikidata_discover import config, llm_helpers
    if client is None:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
    sources, changed = {}, False
    for env, secret in SECRETS:
        value = os.getenv(env)
        sources[env] = "env"
        if not value or (env in _injected and value == _injected[env]):
            name = f"projects/{PROJECT}/secrets/{secret}/versions/latest"
            try:
                value = client.access_secret_version(request={"name": name}).payload.data.decode().strip()
                sources[env] = "secret_manager"
            except Exception as e:  # noqa: BLE001 - one missing optional key must not stop a run
                logger.warning("Secret %s not available (%s: %s); provider %s disabled", secret, type(e).__name__, str(e)[:120], env)
                value = _injected.get(env)  # keep the last good value, if any
                sources[env] = "secret_manager" if value else "missing"
            if value:
                os.environ[env] = value
                _injected[env] = value
        if getattr(config, env, None) != (value or None):
            changed = True
        setattr(config, env, value or None)
    if changed:
        llm_helpers.reset_clients()
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


def export_paths(results_dir: Path, qid: str) -> list:
    """The two files discovery writes only when something is missing or orphaned."""
    return [results_dir / f"missing_divisions_{qid}.csv", results_dir / f"quickstatements_{qid}.qs"]


def clear_stale_exports(bucket, run_id: str, results_dir: Path, run_dir: Path, qid: str) -> int:
    """After an attempt that produced no CSV or QuickStatements file for `qid`, delete the
    ones an earlier attempt may have uploaded, so the bucket never offers statements
    that the latest report contradicts. Returns how many objects were deleted."""
    deleted = 0
    for p in export_paths(results_dir, qid):
        if p.exists():
            continue
        blob = bucket.blob(object_name(run_id, p, results_dir, run_dir))
        if blob.exists():
            blob.delete()
            deleted += 1
    return deleted


def restore_caches(bucket, run_id: str, log_path: Path, results_dir: Path, done: set) -> int:
    """Bring back, from the bucket, the LLM cache files that earlier attempts at still
    pending QIDs read or wrote, so a retry on a fresh instance reuses the same LLM
    answers instead of paying for (and possibly getting) new ones. Returns the count."""
    wanted, attributed = set(), set()
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            names = rec.get("cache_files") or []
            attributed.update(names)
            if rec.get("qid") not in done:
                wanted.update(names)
    # Cache files in the bucket that no log record mentions come from an attempt whose
    # record never reached the bucket (the log upload failed after the artifact upload).
    # They are few, and restoring them keeps that attempt's LLM answers in use.
    prefix = f"runs/{run_id}/cache/"
    for blob in bucket.list_blobs(prefix=prefix):
        name = blob.name[len(prefix):]
        if name and name not in attributed:
            wanted.add(name)
    cache_dir = results_dir / "cache"
    restored = 0
    for name in sorted(wanted):
        local = cache_dir / name
        if local.exists():
            continue
        blob = bucket.blob(prefix + name)
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


def operator_identity() -> str:
    """Who started this run: ACADEMIABOT_OPERATOR if set (the Cloud Function sets it
    from the request), else the git user email, else the OS user name."""
    who = os.getenv("ACADEMIABOT_OPERATOR")
    if who:
        return who
    try:
        who = subprocess.run(["git", "-C", str(RESULTS_DIR.parents[1]), "config", "user.email"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        who = ""
    if who:
        return who
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "unknown"


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

    summary = {"run_id": run_id, "requested": len(qids), "skipped_done": 0, "processed": 0, "ok": 0,
               "failed": 0, "stopped_for_time": False}
    # Bring the run state down from the bucket. If the bucket is unreachable the run
    # still starts from local state and records that, instead of dying silently.
    resumed = (run_dir / "run.json").exists()
    sync_error = None
    try:
        for name in ("log.jsonl", "invocations.jsonl"):
            sync_from_bucket(bucket, run_id, run_dir, name)
        resumed = resumed or bucket.blob(f"runs/{run_id}/run.json").exists()
        restored = restore_caches(bucket, run_id, log_path, results_dir, load_done(log_path))
        if restored:
            report(f"restored {restored} cache files from the bucket for pending universities")
    except Exception as e:  # noqa: BLE001
        sync_error = f"{type(e).__name__}: {str(e)[:120]}"
        report(f"bucket sync failed, continuing from local state: {sync_error}")
        summary["failed"] += 1
    done = load_done(log_path)

    invocation = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git_commit": git_commit(),
                  "operator": operator_identity(),
                  "args": invocation_args if invocation_args is not None else {"argv": sys.argv},
                  "host": os.getenv("K_SERVICE", "local"),
                  "models": {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL, "gemini": config.GEMINI_MODEL},
                  "user_agent": config.USER_AGENT, "qids": qids, "resumed": resumed,
                  "time_budget_s": time_budget_s, "reserve_s": reserve_s, "sync_error": sync_error}
    if not resumed:
        (run_dir / "run.json").write_text(json.dumps({"run_id": run_id, **invocation}, indent=2))
    with (run_dir / "invocations.jsonl").open("a") as f:
        f.write(json.dumps(invocation) + "\n")

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
        for p in export_paths(results_dir, qid):   # outputs of an earlier attempt must not survive
            if p.exists():
                p.unlink()
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
            stale = clear_stale_exports(bucket, run_id, results_dir, run_dir, qid) if rec["status"] == "ok" else 0
            rec["uploaded"] = True
            note = f"uploaded {n} files to gs://{BUCKET}/runs/{run_id}/" + (f", removed {stale} stale export(s)" if stale else "")
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
                correction = {"qid": qid, "status": rec["status"], "uploaded": False,
                              "note": f"run log upload failed: {type(e).__name__}"}
                if "error" in rec:
                    correction["error"] = rec["error"]
                with log_path.open("a") as f:
                    f.write(json.dumps(correction) + "\n")
                rec["uploaded"] = False
                summary["failed"] += 1
        if rec["status"] == "ok" and rec["uploaded"] and not rec.get("unresolved_rows"):
            done.add(qid)
        summary["processed"] += 1
        report(f"[{i}/{len(qids)}] {qid} {rec['status']} {rec.get('label', '')} {rec['seconds']}s")
        report(f"   {note}")
    summary["seconds"] = round(time.time() - started, 1)
    summary["outcome"] = ("stopped_for_time" if summary["stopped_for_time"] else
                          "failed" if summary["failed"] else "ok")
    # Close the invocation record: end time and outcome, then push the run folder once more.
    with (run_dir / "invocations.jsonl").open("a") as f:
        f.write(json.dumps({"started": invocation["started"], "host": invocation["host"],
                            "ended": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "outcome": summary["outcome"], "summary": summary}) + "\n")
    try:
        upload([p for p in run_dir.rglob("*") if p.is_file()])
    except Exception as e:  # noqa: BLE001
        report(f"final run metadata upload failed: {type(e).__name__}: {str(e)[:120]}")
        summary["outcome"] = "failed"
        summary["failed"] = max(summary["failed"], 1)  # usually the same outage already counted
        # The end record above says the earlier outcome; correct it locally so that a
        # later upload (same instance, next slice) carries the true outcome.
        with (run_dir / "invocations.jsonl").open("a") as f:
            f.write(json.dumps({"started": invocation["started"], "host": invocation["host"],
                                "ended": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "outcome": "failed", "summary": summary,
                                "note": f"final run metadata upload failed: {type(e).__name__}"}) + "\n")
    return summary
