"""Batch discovery runner (interim tool until the run log in Anya's week 2 lands).

Usage: python -m wikidata_discover.scripts.batch_collect RUN_ID QID [QID ...]
 - Loads LLM keys from Secret Manager into this process only.
 - Runs Discovery for each QID, continues on failure, appends one JSON line per
   university to results/runs/RUN_ID/log.jsonl, then uploads the run folder.
 - After every university, uploads that university's outputs and the cache entries
   it read or wrote to gs://academiabot/runs/RUN_ID/ so nothing is lost if the
   session dies. A QID counts as done only when discovery and upload both succeeded.
 - Exits nonzero if any university failed or any upload failed.
"""
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "wikidata_discover" / "results"
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


def load_done(log_path: Path) -> set:
    """QIDs whose discovery succeeded and whose files reached the bucket."""
    done = set()
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            try:
                rec = json.loads(line)
                if rec.get("status") == "ok" and rec.get("uploaded"):
                    done.add(rec["qid"])
            except Exception:
                pass
    return done


def load_keys_from_secret_manager():
    from google.cloud import secretmanager
    sm = secretmanager.SecretManagerServiceClient()
    for env, secret in SECRETS:
        if not os.getenv(env):
            name = f"projects/{PROJECT}/secrets/{secret}/versions/latest"
            os.environ[env] = sm.access_secret_version(request={"name": name}).payload.data.decode().strip()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__); return 2
    run_id, qids = argv[0], argv[1:]
    run_dir = RESULTS / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "log.jsonl"
    resumed = (run_dir / "run.json").exists()

    load_keys_from_secret_manager()
    os.environ.setdefault("WD_BOT_USERAGENT", "AcademiaBot/1.0 (ipeirotis@gmail.com)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(run_dir / "pipeline.log"), logging.StreamHandler()])

    from google.cloud import storage
    from wikidata_discover import config, llm_helpers
    from wikidata_discover.discovery import Discovery

    bucket = storage.Client(project=PROJECT).bucket(BUCKET)
    git_commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()

    def upload(paths) -> int:
        n = 0
        for p in paths:
            bucket.blob(f"runs/{run_id}/{p.relative_to(RESULTS)}").upload_from_filename(str(p)); n += 1
        return n

    done = load_done(log_path)
    invocation = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git_commit": git_commit,
                  "command": sys.argv, "models": {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL,
                  "gemini": config.GEMINI_MODEL}, "user_agent": config.USER_AGENT, "qids": qids, "resumed": resumed}
    if not resumed:
        (run_dir / "run.json").write_text(json.dumps({"run_id": run_id, **invocation}, indent=2))
    # Every invocation (first run and each resume) gets its own record, so log.jsonl rows
    # can be attributed to the environment that produced them.
    with (run_dir / "invocations.jsonl").open("a") as f:
        f.write(json.dumps(invocation) + "\n")

    any_failed = False
    for i, qid in enumerate(qids, 1):
        if qid in done:
            print(f"[{i}/{len(qids)}] {qid} already done, skipping", flush=True); continue
        t = time.time()
        rec = {"qid": qid, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        llm_helpers.cache_paths_touched.clear()
        try:
            d = Discovery(qid)
            d.discover_missing()
            rep = RESULTS / "reports" / f"{qid}_report.json"
            report = json.loads(rep.read_text()) if rep.exists() else {}
            rec.update({"status": "ok", "label": getattr(d, "university_label", None), "report": report,
                        "missing_rows": report.get("missing", 0), "orphan_rows": report.get("exists_orphan", 0),
                        "unresolved_rows": report.get("unresolved", 0)})
        except Exception as e:  # noqa: BLE001 - one university must not stop the batch
            rec.update({"status": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}", "trace": traceback.format_exc()[-1500:]})
            any_failed = True
        rec["seconds"] = round(time.time() - t, 1)
        rec["cache_files"] = sorted(p.name for p in llm_helpers.cache_paths_touched)
        try:
            n = upload(artifact_paths(RESULTS, run_dir, qid, since=t, extra=list(llm_helpers.cache_paths_touched)))
            rec["uploaded"] = True
            note = f"uploaded {n} files to gs://{BUCKET}/runs/{run_id}/"
        except Exception as e:  # noqa: BLE001
            rec["uploaded"] = False
            note = f"upload failed: {type(e).__name__}: {str(e)[:120]} (QID will be retried on resume)"
            any_failed = True
        with log_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        # The record itself must reach the bucket too, including for the last QID.
        try:
            upload([p for p in run_dir.rglob("*") if p.is_file()])
        except Exception as e:  # noqa: BLE001
            note += f"; run log upload failed: {type(e).__name__}"
            any_failed = True
        print(f"[{i}/{len(qids)}] {qid} {rec['status']} {rec.get('label', '')} {rec['seconds']}s", flush=True)
        print(f"   {note}", flush=True)
    print("BATCH DONE" + (" WITH FAILURES" if any_failed else ""), flush=True)
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
