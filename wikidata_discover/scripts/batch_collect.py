"""Batch discovery runner (interim tool until the run log in Anya's week 2 lands).

Usage: python -m wikidata_discover.scripts.batch_collect RUN_ID QID [QID ...]

Runs discovery for each QID, resumes from the bucket's copy of the run log, uploads
every university's outputs to gs://academiabot/runs/RUN_ID/ as it goes, and exits
nonzero if anything failed. The same logic runs in the cloud via
wikidata_discover/cloud/collect_function.py. See wikidata_discover/batch.py.
"""
import logging
import sys

from wikidata_discover.batch import (BUCKET, PROJECT, RESULTS_DIR, UnreachableBucket, ensure_user_agent,
                                     load_keys_from_secret_manager, run_batch)
from wikidata_discover.batch import artifact_paths, load_done  # noqa: F401  (re-exported)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__); return 2
    run_id, qids = argv[0], argv[1:]
    run_dir = RESULTS_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(run_dir / "pipeline.log"), logging.StreamHandler()])
    # Keys, user agent, and the bucket client. A failure here is still recorded by
    # run_batch (locally, with a stand-in bucket) instead of leaving no trace.
    fail_reason = None
    try:
        load_keys_from_secret_manager()
        ensure_user_agent()
        from google.cloud import storage
        bucket = storage.Client(project=PROJECT).bucket(BUCKET)
    except Exception as e:  # noqa: BLE001
        fail_reason = f"init: {type(e).__name__}: {str(e)[:200]}"
        print(f"cannot start: {fail_reason}", flush=True)
        bucket = UnreachableBucket(fail_reason)
    summary = run_batch(run_id, qids, bucket, report=lambda s: print(s, flush=True),
                        invocation_args={"argv": ["batch_collect", *argv]}, fail_reason=fail_reason)
    print("BATCH DONE" + (" WITH FAILURES" if summary["failed"] else ""), summary, flush=True)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
