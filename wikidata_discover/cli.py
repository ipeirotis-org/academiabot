import argparse
import logging
from wikidata_discover.discovery import Discovery
from wikidata_discover.harvester import fetch_us_universities
import wikidata_discover.config as config


def run_cli():
    parser = argparse.ArgumentParser(
        description="Wikidata tools: discover divisions or harvest universities"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Can call discover for many QIDs at the same time
    d = sub.add_parser("discover", help="Find missing divisions for a university")
    d.add_argument(
    "university_qids",
    nargs="*",
    help="One or more Wikidata Q-IDs (e.g. Q49210 Q49115 ...). Omit with --batch.",
    )
    d.add_argument("--llm", dest="llm_model", default=None)
    d.add_argument("--debug", action="store_true", help="Enable debug logging")
    d.add_argument(
        "--batch",
        action="store_true",
        help="Process every harvested university (from BigQuery or universities_us.json).",
    )
    d.add_argument(
        "--limit",
        type=int,
        default=None,
        help="In --batch mode, cap how many universities to process this run.",
    )
    d.add_argument(
        "--no-resume",
        action="store_true",
        help="In --batch mode, do not skip universities already in discovery_runs.",
    )
    d.add_argument(
        "--recursive",
        action="store_true",
        help="Discover children recursively. Equivalent to --depth 3 unless --depth is set.",
    )
    d.add_argument(
        "--depth",
        type=int,
        default=None,
        help="Hierarchy depth to discover. 1=schools only, 2=schools+departments, 3=programs/labs/centers.",
    )
    d.add_argument("--no-bq", action="store_true", help="Skip BigQuery writes")

    # harvest subcommand
    h = sub.add_parser("harvest", help="Fetch all U.S. universities to JSON")
    h.add_argument("--no-bq", action="store_true", help="Skip BigQuery writes")

    args = parser.parse_args()

    if args.command == "discover":
        if args.debug:
            logging.basicConfig(level=logging.DEBUG, force=True)
        if args.llm_model:
            config.LLM_MODEL = args.llm_model
        depth = args.depth if args.depth is not None else (3 if args.recursive else 1)
        if depth < 1:
            parser.error("--depth must be 1 or greater")
        if args.batch:
            if args.university_qids:
                parser.error("Do not pass QIDs together with --batch.")
            from wikidata_discover.batch import run_batch_discovery
            run_batch_discovery(
                depth=depth,
                write_bq=not args.no_bq,
                limit=args.limit,
                resume=not args.no_resume,
            )
        else:
            if not args.university_qids:
                parser.error("Provide at least one QID, or use --batch.")
            for qid in args.university_qids:
                Discovery(qid).discover_missing(depth=depth, write_bq=not args.no_bq)

    elif args.command == "harvest":
        fetch_us_universities(write_bq=not args.no_bq)
