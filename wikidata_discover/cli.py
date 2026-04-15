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
        for qid in args.university_qids:
            Discovery(qid).discover_missing()

    elif args.command == "harvest":
        fetch_us_universities()
