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

def upload_results():
    n = 0
    for p in RESULTS.rglob("*"):
        if p.is_file() and p.name != "universities_us.json":
            gcs.blob(f"runs/{run_id}/{p.relative_to(RESULTS)}").upload_from_filename(str(p)); n += 1
    return n

done = set()
if LOG.exists():
    for line in LOG.read_text().splitlines():
        try:
            rec = json.loads(line)
            if rec.get("status") == "ok":
                done.add(rec["qid"])
        except Exception: pass

(RUN_DIR / "run.json").write_text(json.dumps({"run_id": run_id, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "git_commit": git_commit, "models": {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL, "gemini": config.GEMINI_MODEL},
    "user_agent": config.USER_AGENT, "qids": qids}, indent=2))

for i, qid in enumerate(qids, 1):
    if qid in done:
        print(f"[{i}/{len(qids)}] {qid} already done, skipping", flush=True); continue
    t = time.time(); rec = {"qid": qid, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        d = Discovery(qid)
        res = d.discover_missing()
        rep = RESULTS / "reports" / f"{qid}_report.json"
        rec.update({"status": "ok", "label": getattr(d, "university_label", None),
                    "report": json.loads(rep.read_text()) if rep.exists() else None,
                    "missing_or_orphan_rows": len(res)})
    except Exception as e:
        rec.update({"status": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}", "trace": traceback.format_exc()[-1500:]})
    rec["seconds"] = round(time.time() - t, 1)
    with LOG.open("a") as f: f.write(json.dumps(rec) + "\n")
    print(f"[{i}/{len(qids)}] {qid} {rec['status']} {rec.get('label','')} {rec['seconds']}s", flush=True)
    try:
        n = upload_results(); print(f"   uploaded {n} files to gs://academiabot/runs/{run_id}/", flush=True)
    except Exception as e:
        print(f"   upload failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
print("BATCH DONE", flush=True)
