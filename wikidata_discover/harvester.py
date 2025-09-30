import json
from pathlib import Path
from rich.console import Console
from wikidata_discover.sparql_helpers import run_sparql
from wikidata_discover.config import USER_AGENT

console = Console()

# SPARQL to fetch all U.S. universities
_US_UNIV_SPARQL = """
SELECT DISTINCT ?univ ?univLabel ?website WHERE {
  ?univ wdt:P31/wdt:P279* wd:Q3918 ;
         wdt:P17            wd:Q30 .
  OPTIONAL { ?univ wdt:P856 ?website }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY ?univLabel
"""


def fetch_us_universities() -> None:
    console.print("[bold]Querying Wikidata for U.S. universities...[/bold]")
    rows = run_sparql(_US_UNIV_SPARQL)
    rows_tuples = run_sparql(_US_UNIV_SPARQL, as_tuples=True)
    out_path = Path("universities_us.json")
    out_path.write_text(json.dumps(rows_tuples, indent=2))
    console.print(f"[green]Wrote {len(rows)} entries to {out_path}[/green]")

    # also print a summary table
    from rich.table import Table

    table = Table("QID", "Name", "Website", header_style="magenta")
    for b in rows:
        qid = b["univ"]["value"].rsplit("/", 1)[-1]
        name = b["univLabel"]["value"]
        site = b.get("website", {}).get("value", "—")
        table.add_row(qid, name, site)
    console.print(table)
