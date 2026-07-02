import json
import logging
from pathlib import Path
from typing import Optional

from rich.console import Console
from wikidata_discover.sparql_helpers import run_sparql
from wikidata_discover import bq_helpers

console = Console()
logger = logging.getLogger(__name__)

# Default country: United States (Q30). Harvesting is configurable per country.
US_COUNTRY_QID = "Q30"

# Verified institution-identifier property per country. Only entries confirmed
# to be correct Wikidata properties are listed; adding a wrong PID would stamp
# harvested rows with a bad identifier, so unverified countries intentionally
# have no identifier property (harvest still works, identifier stays null).
#   Q30 -> P1771 (IPEDS ID, United States)
# Add more here once verified, e.g. UK UKPRN, EU ETER ID.
COUNTRY_IDENTIFIER_PROPS = {
    US_COUNTRY_QID: "P1771",
}


def build_university_sparql(
    country_qid: str, identifier_prop: Optional[str] = None
) -> str:
    """Build the SPARQL to fetch all universities in a country.

    When ``identifier_prop`` is given, the institution identifier (e.g. IPEDS ID)
    is selected as ``?identifier`` via an OPTIONAL clause.
    """
    identifier_select = " ?identifier" if identifier_prop else ""
    identifier_clause = (
        f"  OPTIONAL {{ ?univ wdt:{identifier_prop} ?identifier }}\n"
        if identifier_prop
        else ""
    )
    return f"""
SELECT DISTINCT ?univ ?univLabel ?website{identifier_select} WHERE {{
  ?univ wdt:P31/wdt:P279* wd:Q3918 ;
         wdt:P17            wd:{country_qid} .
  OPTIONAL {{ ?univ wdt:P856 ?website }}
{identifier_clause}  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY ?univLabel
"""


def _universities_json_path(country_qid: str) -> Path:
    # Keep the historical filename for the U.S. so existing tooling still finds it.
    if country_qid == US_COUNTRY_QID:
        return Path("universities_us.json")
    return Path(f"universities_{country_qid}.json")


def fetch_universities(country_qid: str = US_COUNTRY_QID, write_bq: bool = True) -> None:
    """Harvest all universities in the given country to JSON and BigQuery."""
    identifier_prop = COUNTRY_IDENTIFIER_PROPS.get(country_qid)
    query = build_university_sparql(country_qid, identifier_prop)

    console.print(f"[bold]Querying Wikidata for universities in {country_qid}...[/bold]")
    rows = run_sparql(query)
    rows_tuples = run_sparql(query, as_tuples=True)
    out_path = _universities_json_path(country_qid)
    out_path.write_text(json.dumps(rows_tuples, indent=2))
    console.print(f"[green]Wrote {len(rows)} entries to {out_path}[/green]")

    bq_rows = university_rows_for_bq(rows, country_qid, identifier_prop)
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


def fetch_us_universities(write_bq: bool = True) -> None:
    """Backward-compatible wrapper: harvest U.S. universities (Q30)."""
    fetch_universities(US_COUNTRY_QID, write_bq=write_bq)


def university_rows_for_bq(
    rows: list[dict],
    country_qid: str = US_COUNTRY_QID,
    identifier_prop: Optional[str] = None,
) -> list[dict]:
    harvested_at = bq_helpers.utc_now_iso()
    result = []
    for b in rows:
        identifier = b.get("identifier", {}).get("value") if identifier_prop else None
        result.append(
            {
                "qid": b["univ"]["value"].rsplit("/", 1)[-1],
                "label": b["univLabel"]["value"],
                "website": b.get("website", {}).get("value"),
                "country": country_qid,
                # Keep populating ipeds_id for the U.S. so existing consumers and
                # the IPEDS reconciliation continue to work.
                "ipeds_id": identifier if country_qid == US_COUNTRY_QID else None,
                "identifier": identifier,
                "identifier_property": identifier_prop,
                "harvested_at": harvested_at,
            }
        )
    return result
