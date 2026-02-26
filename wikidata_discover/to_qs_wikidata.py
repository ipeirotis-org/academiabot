import json
from pathlib import Path
from wikidata_discover.config import console

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

def export_quickstatements(missing, university_qid, university_label, max_items=None):
    """
    Export missing or orphan divisions into QuickStatements format.

    Args:
        max_items: Optional cap on how many items to export. None means all.
    """

    qs_lines = []
    items = missing[:max_items] if max_items else missing

    for item in items:
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
            f"LAST|P361|{university_qid}",
            ""
        ])

    out_path = Path(f"quickstatements_{university_qid}.qs")
    out_path.write_text("\n".join(qs_lines))
    console.print(f"[green]QuickStatements file written → {out_path}[/green]")

    return out_path