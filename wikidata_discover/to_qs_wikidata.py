import json
from pathlib import Path
from wikidata_discover.config import console, RESULTS_DIR

TYPE_MAP = {
    "department": "Q2467461",
    "dept": "Q2467461",
    "school": "Q31855",
    "college": "Q31855",
    "faculty": "Q31855",
    "division": "Q576104",
    "campus": "Q33506",
    None: "Q2467461",
}

# Property used to link a unit to its university. Known issue 11 in AGENTS.md:
# the data model says P749 is primary; switch this constant when the exporter is
# reworked (Anya's week 9 in TASKS.md).
PARENT_PROPERTY = "P361"


def quickstatements_lines(missing, university_qid, university_label, max_items=None):
    """Build QuickStatements lines for a discovery result. Pure function, unit tested.

    Items with status "orphan" already exist in Wikidata (item["qid"]); they get a
    single statement linking the existing item to the university. Every other item
    is created as a new entity.
    """
    qs_lines = []
    # Unresolved rows (Wikidata could not be checked) are never exported, and they must
    # not consume the export cap either.
    exportable = [it for it in missing if it.get("status") != "unresolved"]
    items = exportable[:max_items] if max_items else exportable

    for item in items:
        if item.get("status") == "orphan" and item.get("qid"):
            qs_lines.extend([f"{item['qid']}|{PARENT_PROPERTY}|{university_qid}", ""])
            continue

        name = (item["name"] or "").replace('"', '\\"')

        unit_type = (item.get("unit_type") or "").lower()
        unit_type_safe = unit_type.replace('"', '\\"')

        description = (
            f"{unit_type_safe} within {university_label}"
            if unit_type_safe
            else f"organizational unit within {university_label}"
        ).replace('"', '\\"')

        type_qid = TYPE_MAP.get(unit_type, TYPE_MAP[None])

        qs_lines.extend([
            "CREATE",
            f'LAST|Len|"{name}"',
            f'LAST|Den|"{description}"',
            f"LAST|P31|{type_qid}",
            f"LAST|{PARENT_PROPERTY}|{university_qid}",
            ""
        ])
    return qs_lines

def export_quickstatements(missing, university_qid, university_label, max_items=None, out_dir=None):
    """
    Export missing or orphan divisions into QuickStatements format.

    Args:
        max_items: Optional cap on how many items to export. None means all.
        out_dir: Directory for the .qs file. Defaults to RESULTS_DIR.
    """

    qs_lines = quickstatements_lines(missing, university_qid, university_label, max_items)

    out_dir = Path(out_dir) if out_dir else RESULTS_DIR
    out_path = out_dir / f"quickstatements_{university_qid}.qs"
    if not qs_lines:
        # Only unresolved rows: nothing may be uploaded, so no file is written and
        # none from an earlier run is left behind.
        if out_path.exists():
            out_path.unlink()
        console.print("[yellow]No QuickStatements written: every row is unresolved.[/yellow]")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(qs_lines))
    console.print(f"[green]QuickStatements file written → {out_path}[/green]")

    return out_path