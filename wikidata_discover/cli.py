import argparse
import logging
import sys
from wikidata_discover.discovery import Discovery
from wikidata_discover.harvester import fetch_us_universities
from wikidata_discover import llm_helpers, run_log
import wikidata_discover.config as config


def run_config(key_sources: dict) -> dict:
    """The configuration recorded in the runs row: providers, models, depth, and where
    each key came from (never the key itself)."""
    return {"providers": llm_helpers.available_providers(),
            "models": {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL,
                       "gemini": config.GEMINI_MODEL},
            "depth": 1, "key_sources": key_sources, "user_agent": config.USER_AGENT,
            "dataset": config.RUNLOG_DATASET, "bucket": config.RUNLOG_BUCKET}


def discover_with_run_log(qids, argv) -> None:
    """Run discovery for each QID and record the run in the run log."""
    from wikidata_discover.batch import operator_identity, source_identity, RESULTS_DIR
    key_sources = config.load_api_keys()
    run_id = run_log.new_run_id("discover")
    run_dir = RESULTS_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    identity = source_identity(run_dir)
    log = run_log.start(run_id, command="discover", args={"argv": argv}, qids=qids,
                        who=operator_identity(), git_commit=identity, run_config=run_config(key_sources))
    patch = run_dir / f"source-{identity}.patch"
    if patch.exists():
        log.run_row["source_patch_path"] = log.save_text(patch.name, patch.read_text())
    outcome, done = "failed", []
    try:
        for qid in qids:
            try:
                Discovery(qid).discover_missing()
                done.append(qid)
            finally:
                log.flush()
        outcome = "ok"
    finally:
        status = log.finish(outcome, {"qids_done": done})
        config.console.print(f"[dim]{run_log.describe(status)}[/dim]")


def run_cli():
    parser = argparse.ArgumentParser(
        description="Wikidata tools: discover divisions or harvest universities"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Can call discover for many QIDs at the same time
    d = sub.add_parser("discover", help="Find missing divisions for a university")
    d.add_argument(
    "university_qids",
    nargs="+",
    help="One or more Wikidata Q-IDs (e.g. Q49210 Q49115 ...)",
    )
    d.add_argument("--llm", dest="llm_model", default=None)
    d.add_argument("--debug", action="store_true", help="Enable debug logging")

    # harvest subcommand
    h = sub.add_parser("harvest", help="Fetch all U.S. universities to JSON")

    args = parser.parse_args()

    if args.command == "discover":
        if args.debug:
            logging.basicConfig(level=logging.DEBUG, force=True)
        if args.llm_model:
            config.LLM_MODEL = args.llm_model
        discover_with_run_log(args.university_qids, sys.argv[1:])

    elif args.command == "harvest":
        fetch_us_universities()
