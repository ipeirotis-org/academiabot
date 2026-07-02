"""IPEDS reconciliation: match IPEDS institutions against Wikidata by P1771.

Loads an IPEDS institutional-characteristics (HD) CSV, fetches every Wikidata
entity carrying an IPEDS ID (P1771), and reports which IPEDS institutions are
already in Wikidata and which are missing entirely. Results are written locally
and to the BigQuery ``ipeds_reconciliation`` table.

The IPEDS HD file is published by NCES (https://nces.ed.gov/ipeds/use-the-data);
download it and pass the path with ``--csv``. Column names follow the HD layout:
UNITID (the IPEDS ID), INSTNM (institution name), WEBADDR (website).
"""

import io
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from wikidata_discover import bq_helpers
from wikidata_discover.config import console
from wikidata_discover.sparql_helpers import execute_sparql_bindings

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "results"

WIKIDATA_IPEDS_SPARQL = """
SELECT ?item ?ipeds WHERE {
  ?item wdt:P1771 ?ipeds .
}
"""

MATCHED = "matched"
MISSING = "missing_from_wikidata"


def normalize_ipeds_id(value: Any) -> Optional[str]:
    """Normalize an IPEDS UNITID to a canonical digit string (no leading zeros)."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    return str(int(digits))


def parse_ipeds_csv(source: Any) -> List[Dict[str, Any]]:
    """Parse an IPEDS HD CSV (path, file-like, or raw text) into institution rows.

    Returns rows of {ipeds_id, name, website}. Rows without a usable UNITID are
    skipped. Column lookup is case-insensitive to tolerate layout variations.
    """
    if isinstance(source, (str, Path)) and Path(str(source)).exists():
        # IPEDS files are commonly latin-1 encoded.
        frame = pd.read_csv(source, dtype=str, encoding="latin-1")
    elif isinstance(source, str):
        frame = pd.read_csv(io.StringIO(source), dtype=str)
    else:
        frame = pd.read_csv(source, dtype=str)

    return _rows_from_frame(frame)


def _rows_from_frame(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    columns = {col.lower(): col for col in frame.columns}
    unitid_col = columns.get("unitid")
    name_col = columns.get("instnm") or columns.get("name")
    web_col = columns.get("webaddr") or columns.get("website")
    if not unitid_col:
        raise ValueError("IPEDS CSV is missing a UNITID column")

    rows: List[Dict[str, Any]] = []
    for _, record in frame.iterrows():
        ipeds_id = normalize_ipeds_id(record.get(unitid_col))
        if not ipeds_id:
            continue
        rows.append(
            {
                "ipeds_id": ipeds_id,
                "name": _clean(record.get(name_col)) if name_col else None,
                "website": _clean(record.get(web_col)) if web_col else None,
            }
        )
    return rows


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def fetch_wikidata_ipeds_map() -> Dict[str, str]:
    """Return a map of normalized IPEDS ID -> Wikidata QID for all P1771 claims."""
    bindings = execute_sparql_bindings(WIKIDATA_IPEDS_SPARQL)
    mapping: Dict[str, str] = {}
    for binding in bindings:
        ipeds_id = normalize_ipeds_id(binding.get("ipeds", {}).get("value"))
        qid = binding.get("item", {}).get("value", "").rsplit("/", 1)[-1]
        if ipeds_id and qid and ipeds_id not in mapping:
            mapping[ipeds_id] = qid
    return mapping


def reconcile_ipeds(
    ipeds_rows: List[Dict[str, Any]],
    wikidata_ipeds_map: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Classify each IPEDS institution as matched or missing from Wikidata."""
    results: List[Dict[str, Any]] = []
    for row in ipeds_rows:
        ipeds_id = row["ipeds_id"]
        qid = wikidata_ipeds_map.get(ipeds_id)
        results.append(
            {
                "ipeds_id": ipeds_id,
                "name": row.get("name"),
                "website": row.get("website"),
                "matched_qid": qid,
                "status": MATCHED if qid else MISSING,
            }
        )
    return results


def summarize(results: List[Dict[str, Any]]) -> Dict[str, int]:
    matched = sum(1 for r in results if r["status"] == MATCHED)
    return {
        "total": len(results),
        "matched": matched,
        "missing_from_wikidata": len(results) - matched,
    }


def run_ipeds_reconciliation(
    csv_path: str,
    write_bq: bool = True,
    out_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile an IPEDS HD CSV against Wikidata and persist the results."""
    console.print(f"[bold]Loading IPEDS institutions from {csv_path}[/bold]")
    ipeds_rows = parse_ipeds_csv(csv_path)
    console.print(f"[dim]Parsed {len(ipeds_rows)} IPEDS institutions.[/dim]")

    console.print("[bold]Fetching Wikidata P1771 (IPEDS ID) claims...[/bold]")
    wikidata_map = fetch_wikidata_ipeds_map()
    console.print(f"[dim]Found {len(wikidata_map)} Wikidata entities with an IPEDS ID.[/dim]")

    results = reconcile_ipeds(ipeds_rows, wikidata_map)
    counts = summarize(results)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = out_path or (RESULTS_DIR / "ipeds_reconciliation.csv")
    pd.DataFrame(results).to_csv(path, index=False)
    (RESULTS_DIR / "ipeds_reconciliation_summary.json").write_text(
        json.dumps(counts, indent=2)
    )
    console.print(
        f"[green]Reconciliation: {counts['matched']} matched, "
        f"{counts['missing_from_wikidata']} missing from Wikidata "
        f"(of {counts['total']}). Written to {path}[/green]"
    )

    if write_bq and results:
        reconciled_at = bq_helpers.utc_now_iso()
        bq_rows = [dict(row, reconciled_at=reconciled_at) for row in results]
        if bq_helpers.try_save_ipeds_reconciliation(bq_rows):
            console.print(
                f"[green]Saved {len(bq_rows)} reconciliation rows to BigQuery.[/green]"
            )
        else:
            console.print("[yellow]BigQuery unavailable; kept local CSV only.[/yellow]")

    return {"counts": counts, "path": str(path)}
