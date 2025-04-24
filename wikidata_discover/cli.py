import argparse
from discovery import Discovery
from harvester import fetch_us_universities
from config import LLM_MODEL

def run_cli():
    parser = argparse.ArgumentParser(
        description="Wikidata tools: discover divisions or harvest universities"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # discover subcommand
    d = sub.add_parser("discover", help="Find missing divisions for a university")
    d.add_argument("university_qid", help="Wikidata Q-ID (e.g. Q49210)")
    d.add_argument("--llm", dest="llm_model", default=LLM_MODEL)

    # harvest subcommand
    h = sub.add_parser("harvest", help="Fetch all U.S. universities to JSON")

    args = parser.parse_args()

    if args.command == "discover":
        from config import LLM_MODEL as DEFAULT_MODEL
        from config import console
        # override model if requested
        if args.llm_model != DEFAULT_MODEL:
            import config
            config.LLM_MODEL = args.llm_model
        Discovery(args.university_qid).discover_missing()

    elif args.command == "harvest":
        fetch_us_universities()
