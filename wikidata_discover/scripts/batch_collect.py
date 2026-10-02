"""Batch discovery runner (interim tool until the run log in Anya's week 2 lands).

Usage: python -m wikidata_discover.scripts.batch_collect RUN_ID QID [QID ...]
 - Loads LLM keys from Secret Manager into this process only.
 - Runs Discovery for each QID, continues on failure, appends one JSON line per
   university to results/runs/RUN_ID/log.jsonl.
 - After every university, uploads results/ (CSV, QS, reports, cache, run log) to
   gs://academiabot/runs/RUN_ID/ so nothing is lost if the session dies.
"""
import os, sys, time, json, traceback, hashlib, subprocess
from pathlib import Path
from google.cloud import secretmanager, storage

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "wikidata_discover" / "results"
run_id, qids = sys.argv[1], sys.argv[2:]
RUN_DIR = RESULTS / "runs" / run_id; RUN_DIR.mkdir(parents=True, exist_ok=True)
LOG = RUN_DIR / "log.jsonl"

sm = secretmanager.SecretManagerServiceClient()
for env, secret in [("OPENAI_API_KEY","openai-api-key"),("ANTHROPIC_API_KEY","anthropic-api-key"),("GOOGLE_API_KEY","gemini-api-key")]:
    if not os.getenv(env):
        os.environ[env] = sm.access_secret_version(request={"name": f"projects/wikidata-academia/secrets/{secret}/versions/latest"}).payload.data.decode().strip()
os.environ.setdefault("WD_BOT_USERAGENT", "AcademiaBot/1.0 (ipeirotis@gmail.com)")

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.FileHandler(RUN_DIR / "pipeline.log"), logging.StreamHandler()])
from wikidata_discover.discovery import Discovery
from wikidata_discover import config

gcs = storage.Client(project="wikidata-academia").bucket("academiabot")
git_commit = subprocess.run(["git","-C",str(REPO),"rev-parse","--short","HEAD"],capture_output=True,text=True).stdout.strip()

INVOCATION_STARTED = time.time()


def artifact_paths(qid: str, since: float):
    """Exact files this invocation may have written for one QID: the run folder, the
    QID's own CSV, QuickStatements file and report, and cache entries. Only files
    modified at or after `since` count, so stale outputs from earlier runs for the
    same QID are never attributed to this run."""
    exact = [
        RESULTS / f"missing_divisions_{qid}.csv",
        RESULTS / f"quickstatements_{qid}.qs",
        RESULTS / "reports" / f"{qid}_report.json",
    ]
    cache = [p for p in (RESULTS / "cache").glob("*.json") if p.is_file()]
    fresh = [p for p in exact + cache if p.is_file() and p.stat().st_mtime >= since]
    return [p for p in RUN_DIR.rglob("*") if p.is_file()] + fresh


def upload_results(qid: str, since: float):
    n = 0
    for p in artifact_paths(qid, since):
        gcs.blob(f"runs/{run_id}/{p.relative_to(RESULTS)}").upload_from_filename(str(p)); n += 1
    return n

done = set()
if LOG.exists():
    for line in LOG.read_text().splitlines():
        try:
            rec = json.loads(line)
            # A QID is finished only when discovery succeeded AND its files reached the bucket.
            if rec.get("status") == "ok" and rec.get("uploaded"):
                done.add(rec["qid"])
        except Exception: pass

invocation = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git_commit": git_commit,
              "command": sys.argv, "models": {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL, "gemini": config.GEMINI_MODEL},
              "user_agent": config.USER_AGENT, "qids": qids, "resumed": bool(done)}
if not (RUN_DIR / "run.json").exists():
    (RUN_DIR / "run.json").write_text(json.dumps({"run_id": run_id, **invocation}, indent=2))
# Every invocation (first run and each resume) gets its own record, so log.jsonl rows
# can be attributed to the environment that produced them.
with (RUN_DIR / "invocations.jsonl").open("a") as f:
    f.write(json.dumps(invocation) + "\n")

any_failed = False
for i, qid in enumerate(qids, 1):
    if qid in done:
        print(f"[{i}/{len(qids)}] {qid} already done, skipping", flush=True); continue
    t = time.time(); rec = {"qid": qid, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        d = Discovery(qid)
        res = d.discover_missing()
        rep = RESULTS / "reports" / f"{qid}_report.json"
        report = json.loads(rep.read_text()) if rep.exists() else {}
        rec.update({"status": "ok", "label": getattr(d, "university_label", None), "report": report,
                    "missing_rows": report.get("missing", 0), "orphan_rows": report.get("exists_orphan", 0),
                    "unresolved_rows": report.get("unresolved", 0)})
    except Exception as e:
        rec.update({"status": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}", "trace": traceback.format_exc()[-1500:]})
        any_failed = True
    rec["seconds"] = round(time.time() - t, 1)
    try:
        n = upload_results(qid, since=t)
        rec["uploaded"] = True
        upload_note = f"uploaded {n} files to gs://academiabot/runs/{run_id}/"
    except Exception as e:
        rec["uploaded"] = False
        upload_note = f"upload failed: {type(e).__name__}: {str(e)[:120]} (QID will be retried on resume)"
        any_failed = True
    with LOG.open("a") as f: f.write(json.dumps(rec) + "\n")
    print(f"[{i}/{len(qids)}] {qid} {rec['status']} {rec.get('label','')} {rec['seconds']}s", flush=True)
    print(f"   {upload_note}", flush=True)
print("BATCH DONE" + (" WITH FAILURES" if any_failed else ""), flush=True)
sys.exit(1 if any_failed else 0)
