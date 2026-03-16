"""
Aggregate QA reports for the Phase 1 pilot universities into a summary table.

Usage:
    python -m wikidata_discover.scripts.pilot_summary

Reads all <QID>_report.json files from results/reports/ and prints a rich
table with per-university metrics plus totals.

Metrics:
    candidates  -- units the LLM extracted
    wikidata    -- direct children already in Wikidata
    linked      -- LLM candidates matched to existing Wikidata children
    orphan      -- LLM candidates found in Wikidata but missing parent link
    missing     -- LLM candidates not found in Wikidata at all
    recall      -- linked / wikidata (fraction of known children LLM recovered)
"""

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

PILOT_QIDS = [
    "Q49210",   # NYU
    "Q49088",   # Columbia
    "Q49108",   # MIT
    "Q41506",   # Stanford
    "Q168756",  # UC Berkeley
    "Q230492",  # U Michigan
    "Q1640613", # Howard
    "Q131252",  # Caltech
    "Q579968",  # U Texas Austin
    "Q1143289", # CUNY
]

REPORTS_DIR = Path(__file__).parent.parent / "results" / "reports"

console = Console()


def load_reports() -> list[dict]:
    reports = []
    for qid in PILOT_QIDS:
        path = REPORTS_DIR / f"{qid}_report.json"
        if path.exists():
            reports.append(json.loads(path.read_text()))
        else:
            console.print(f"[yellow]Missing report for {qid} -- run discover first.[/yellow]")
    return reports


def print_summary(reports: list[dict]) -> None:
    table = Table(title="Pilot Summary -- 10 Universities", show_footer=True)
    table.add_column("University", footer="TOTAL")
    table.add_column("QID")
    table.add_column("Candidates", justify="right", footer="")
    table.add_column("Wikidata", justify="right", footer="")
    table.add_column("Linked", justify="right", footer="")
    table.add_column("Orphan", justify="right", footer="")
    table.add_column("Missing", justify="right", footer="")
    table.add_column("Recall", justify="right", footer="")

    totals = {k: 0 for k in ("total_candidates", "total_wikidata_children", "exists_linked", "exists_orphan", "missing")}

    for r in reports:
        recall = r.get("recall")
        recall_str = f"{recall:.1%}" if recall is not None else "n/a"
        table.add_row(
            r["university_label"],
            r["university_qid"],
            str(r["total_candidates"]),
            str(r["total_wikidata_children"]),
            str(r["exists_linked"]),
            str(r["exists_orphan"]),
            str(r["missing"]),
            recall_str,
        )
        for k in totals:
            totals[k] += r.get(k, 0)

    overall_recall = (
        totals["exists_linked"] / totals["total_wikidata_children"]
        if totals["total_wikidata_children"] > 0
        else None
    )
    overall_recall_str = f"{overall_recall:.1%}" if overall_recall is not None else "n/a"

    # Update footer columns
    table.columns[2]._footer = str(totals["total_candidates"])
    table.columns[3]._footer = str(totals["total_wikidata_children"])
    table.columns[4]._footer = str(totals["exists_linked"])
    table.columns[5]._footer = str(totals["exists_orphan"])
    table.columns[6]._footer = str(totals["missing"])
    table.columns[7]._footer = overall_recall_str

    console.print(table)
    console.print(
        f"\n[bold]Notes:[/bold]\n"
        f"  recall  = linked / wikidata (fraction of known Wikidata children the LLM recovered)\n"
        f"  missing = LLM candidates not in Wikidata (may be real but unadded, or hallucinations)\n"
        f"  orphan  = found in Wikidata but parent link missing\n"
        f"\n[dim]Reports read from: {REPORTS_DIR}[/dim]"
    )


def main() -> None:
    reports = load_reports()
    if not reports:
        console.print("[red]No reports found. Run discover on the pilot universities first:[/red]")
        console.print(
            "  python -m wikidata_discover.scripts.wikidata_division_discover discover "
            + " ".join(PILOT_QIDS)
        )
        return
    print_summary(reports)


if __name__ == "__main__":
    main()
