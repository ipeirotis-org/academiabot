import json
import logging
from pathlib import Path
from rich.console import Console
from wikidata_discover.sparql_helpers import run_sparql
from wikidata_discover import bq_helpers

console = Console()
logger = logging.getLogger(__name__)

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


def fetch_us_universities(write_bq: bool = True) -> None:
    console.print("[bold]Querying Wikidata for U.S. universities...[/bold]")
    rows = run_sparql(_US_UNIV_SPARQL)
    rows_tuples = run_sparql(_US_UNIV_SPARQL, as_tuples=True)
    out_path = Path("universities_us.json")
    out_path.write_text(json.dumps(rows_tuples, indent=2))
    console.print(f"[green]Wrote {len(rows)} entries to {out_path}[/green]")

    bq_rows = university_rows_for_bq(rows)
    if write_bq:
        if bq_helpers.try_save_universities(bq_rows):
            console.print(f"[green]Saved {len(bq_rows)} universities to BigQuery.[/green]")
        else:
            console.print("[yellow]BigQuery unavailable; kept local JSON output only.[/yellow]")

    # also print a summary table
    from rich.table import Table

    table = Table("QID", "Name", "Website", header_style="magenta")
    for b in rows:
        qid = b["univ"]["value"].rsplit("/", 1)[-1]
        name = b["univLabel"]["value"]
        site = b.get("website", {}).get("value", "—")
        table.add_row(qid, name, site)
    console.print(table)


def university_rows_for_bq(rows: list[dict]) -> list[dict]:
    harvested_at = bq_helpers.utc_now_iso()
    result = []
    for b in rows:
        result.append(
            {
                "qid": b["univ"]["value"].rsplit("/", 1)[-1],
                "label": b["univLabel"]["value"],
                "website": b.get("website", {}).get("value"),
                "country": "Q30",
                "ipeds_id": None,
                "harvested_at": harvested_at,
            }
        )
    return result
