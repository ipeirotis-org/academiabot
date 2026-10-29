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
            "depth": 1, "only_provider": config.ONLY_PROVIDER, "key_sources": key_sources,
            "user_agent": config.USER_AGENT,
            "dataset": config.RUNLOG_DATASET, "bucket": config.RUNLOG_BUCKET}


def check_only_provider() -> None:
    """Stop before any work when --provider names a provider that has no key."""
    if config.ONLY_PROVIDER and not llm_helpers.available_providers().get(config.ONLY_PROVIDER):
        raise SystemExit(f"--provider {config.ONLY_PROVIDER}: no API key for it in the environment, "
                         ".env, or Secret Manager. Nothing was run.")


def discover_with_run_log(qids, argv) -> None:
    """Run discovery for each QID and record the run in the run log."""
    from wikidata_discover.batch import operator_identity, source_identity, RESULTS_DIR
    key_sources = config.load_api_keys()
    check_only_provider()
    run_id = run_log.new_run_id("discover")
    run_dir = RESULTS_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    identity = source_identity(run_dir)
    log = run_log.start(run_id, command="discover", args={"argv": argv}, qids=qids,
                        who=operator_identity(), git_commit=identity, run_config=run_config(key_sources))
    log.attach_source_patch(run_dir, identity)
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


def subunits_with_run_log(qids, argv) -> None:
    """Ask for the sub-units of each QID and record the run in the run log."""
    from wikidata_discover import subunits
    from wikidata_discover.batch import operator_identity, source_identity, RESULTS_DIR
    key_sources = config.load_api_keys()
    check_only_provider()
    run_id = run_log.new_run_id("subunits")
    run_dir = RESULTS_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    identity = source_identity(run_dir)
    log = run_log.start(run_id, command="subunits", args={"argv": argv}, qids=qids,
                        who=operator_identity(), git_commit=identity, run_config=run_config(key_sources))
    log.attach_source_patch(run_dir, identity)
    outcome, done = "failed", []
    try:
        for qid in qids:
            try:
                s = subunits.subunits_for(qid, log)
                done.append(s)
                config.console.print(f"{qid} {s['label']}: {s['units']} sub-units from {s['provider']} "
                                     f"{s['by_type']}, {s['no_url']} without a URL. Wrote {s['csv']}")
            finally:
                log.flush()
        outcome = "ok"
    finally:
        status = log.finish(outcome, {"done": done})
        config.console.print(f"[dim]{run_log.describe(status)}[/dim]")


def snapshot_command(args, argv) -> None:
    from wikidata_discover import snapshot
    from wikidata_discover.batch import is_qid
    qids = list(args.university_qids)
    bad = [q for q in qids if not is_qid(q)]
    if bad or (not qids and not args.all) or (qids and args.all):
        raise SystemExit("snapshot: give Q-IDs such as Q49210, or --all (not both). Bad: " + ", ".join(bad))
    if args.all:
        from google.cloud import storage
        qids = snapshot.load_university_list(storage.Client(project=config.GCP_PROJECT).bucket(config.RUNLOG_BUCKET))
    snapshot.run_snapshots(qids, args.run_id, argv, report=config.console.print)


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
    d.add_argument("--provider", choices=config.PROVIDERS, default=None,
                   help="Run one AI alone, for extraction and matching. Default: the first that answers")
    d.add_argument("--debug", action="store_true", help="Enable debug logging")

    u = sub.add_parser("subunits", help="Ask an AI for the sub-units of a university or school")
    u.add_argument("unit_qids", nargs="+", help="Q-IDs of units (e.g. Q770467 for NYU Stern)")
    u.add_argument("--provider", choices=config.PROVIDERS, default=None,
                   help="Run one AI alone. Default: Anthropic, then OpenAI, then Gemini")
    u.add_argument("--max-searches", type=int, default=None,
                   help="Web searches per request (Anthropic and OpenAI). Default: 5")
    u.add_argument("--debug", action="store_true", help="Enable debug logging")

    # harvest subcommand
    h = sub.add_parser("harvest", help="Fetch all U.S. universities to JSON")

    s = sub.add_parser("snapshot", help="Save what Wikidata has below a university")
    s.add_argument("university_qids", nargs="*", help="Q-IDs to snapshot (e.g. Q49210)")
    s.add_argument("--all", action="store_true",
                   help="Every university in gs://academiabot/universities_us_tier1.json")
    s.add_argument("--run-id", default=None,
                   help="Fixed run id. A restart with the same id skips finished universities")

    args = parser.parse_args()

    if args.command == "discover":
        if args.debug:
            logging.basicConfig(level=logging.DEBUG, force=True)
        if args.llm_model:
            config.LLM_MODEL = args.llm_model
        config.ONLY_PROVIDER = args.provider
        discover_with_run_log(args.university_qids, sys.argv[1:])

    elif args.command == "subunits":
        from wikidata_discover.batch import is_qid
        bad = [q for q in args.unit_qids if not is_qid(q)]
        if bad:
            raise SystemExit("subunits: give Q-IDs such as Q770467. Bad: " + ", ".join(bad))
        if args.debug:
            logging.basicConfig(level=logging.DEBUG, force=True)
        config.ONLY_PROVIDER = args.provider
        if args.max_searches:
            llm_helpers.SUBUNIT_MAX_SEARCHES = args.max_searches
        subunits_with_run_log(args.unit_qids, sys.argv[1:])

    elif args.command == "harvest":
        fetch_us_universities()

    elif args.command == "snapshot":
        snapshot_command(args, sys.argv[1:])
