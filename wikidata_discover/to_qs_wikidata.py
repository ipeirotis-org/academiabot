from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from wikidata_discover.config import console


# Q43229 = organization, the generic fallback. Several unit types intentionally
# map to it because no verified class QID is known yet: emitting a specific but
# wrong QID would stamp every uploaded entity with an incorrect P31 (e.g.
# Q1664727 is "Institute of Christ the King Sovereign Priest", Q576104 is
# "neonate", Q33506 is "museum" -- none are organizational-unit classes). Prefer
# a generic-but-correct class over a specific-but-wrong one until verified
# mappings are supplied.
GENERIC_UNIT_QID = "Q43229"
TYPE_MAP = {
    "school": "Q31855",
    "college": "Q31855",
    "faculty": "Q31855",
    "division": GENERIC_UNIT_QID,
    "campus": GENERIC_UNIT_QID,
    "department": "Q2467461",
    "dept": "Q2467461",
    "academic department": "Q2467461",
    "program": GENERIC_UNIT_QID,
    "academic program": GENERIC_UNIT_QID,
    "degree program": GENERIC_UNIT_QID,
    "lab": "Q483242",
    "laboratory": "Q483242",
    "center": "Q7315155",
    "centre": "Q7315155",
    "research center": "Q7315155",
    "research centre": "Q7315155",
    "institute": GENERIC_UNIT_QID,
    "unit": GENERIC_UNIT_QID,
    None: GENERIC_UNIT_QID,
}

CREATE_STATUSES = {"missing"}
# "linked_joint": an existing unit already linked to the current parent that is
# also cross-listed, so it still needs P749 statements for its other parents.
LINK_STATUSES = {"orphan", "exists_orphan", "linked_joint"}


def export_quickstatements(
    missing: List[Dict[str, Any]],
    university_qid: str,
    university_label: str,
    max_items: Optional[int] = None,
    out_path: Optional[Path] = None,
    validate: bool = True,
) -> Path:
    """
    Export missing or orphan divisions into QuickStatements format.

    Args:
        max_items: Optional cap on how many items to export. None means all.
        out_path: Optional explicit output path.
        validate: When True, run ShEx validation and drop any block that fails
            the schema (shapes/academia.shex) before writing.
    """
    qs_lines = build_quickstatements(
        missing,
        university_qid=university_qid,
        university_label=university_label,
        max_items=max_items,
    )

    if validate:
        qs_lines = validate_and_report(qs_lines)

    path = out_path or Path(f"quickstatements_{university_qid}.qs")
    path.write_text("\n".join(qs_lines))
    console.print(f"[green]QuickStatements file written to {path}[/green]")
    return path


def validate_and_report(qs_lines: List[str]) -> List[str]:
    """Validate QS lines against the ShEx schema, warn on drops, return valid lines."""
    # Imported here to avoid a circular import (shex_validation imports TYPE_MAP).
    from wikidata_discover.shex_validation import filter_valid_quickstatements

    valid_lines, report = filter_valid_quickstatements(qs_lines)
    if report.invalid:
        console.print(
            f"[yellow]ShEx validation dropped {len(report.invalid)} of "
            f"{report.total_blocks} block(s):[/yellow]"
        )
        for _, describe, violations in report.invalid:
            console.print(f"[yellow]  - {describe}: {'; '.join(violations)}[/yellow]")
    return valid_lines


def build_quickstatements(
    items: List[Dict[str, Any]],
    university_qid: str,
    university_label: str,
    max_items: Optional[int] = None,
) -> List[str]:
    """
    Build QuickStatements lines for missing entities and existing orphan entities.

    Missing rows create an entity with P31 and P749. Orphan rows add P749 to the
    matched QID. Recursive rows should provide parent_qid and parent_label.
    """
    qs_lines: List[str] = []
    selected_items = items[:max_items] if max_items else items

    for item in selected_items:
        status = normalize_status(item.get("status"))
        if status not in CREATE_STATUSES | LINK_STATUSES:
            continue

        # For an already-linked cross-listed unit, the current parent link
        # already exists in Wikidata; only emit its remaining (joint) parents.
        include_direct_parent = status != "linked_joint"
        parent_specs = parent_specs_for_item(
            item, university_qid, include_direct_parent=include_direct_parent
        )
        if not parent_specs:
            continue

        if status in CREATE_STATUSES:
            qs_lines.extend(create_entity_lines(item, parent_specs, university_label))
        else:
            qid = clean_qid(item.get("qid") or item.get("matched_qid"))
            if not qid:
                continue
            qs_lines.extend(link_existing_entity_lines(qid, parent_specs))

    return qs_lines


def create_entity_lines(
    item: Dict[str, Any],
    parent_specs: List[Dict[str, Any]],
    university_label: str,
) -> List[str]:
    name = escape_qs_string(item.get("name") or item.get("unit_name") or "")
    if not name:
        return []

    unit_type = normalize_unit_type(item.get("unit_type"))
    parent_label = item.get("parent_label") or university_label
    description = escape_qs_string(f"{unit_type} within {parent_label}")
    type_qid = type_qid_for(unit_type)

    lines = [
        "CREATE",
        f'LAST|Len|"{name}"',
        f'LAST|Den|"{description}"',
        f"LAST|P31|{type_qid}",
    ]

    website = normalize_website(item.get("url") or item.get("website"))
    if website:
        lines.append(f'LAST|P856|"{escape_qs_string(website)}"')

    for parent in parent_specs:
        lines.append(parent_statement("LAST", parent))

    lines.append("")
    return lines


def link_existing_entity_lines(qid: str, parent_specs: List[Dict[str, Any]]) -> List[str]:
    lines = [parent_statement(qid, parent) for parent in parent_specs]
    if lines:
        lines.append("")
    return lines


def parent_statement(subject: str, parent_spec: Dict[str, Any]) -> str:
    parent_qid = clean_qid(parent_spec.get("qid"))
    line = f"{subject}|P749|{parent_qid}"
    for qualifier in parent_spec.get("qualifiers", []):
        prop = qualifier.get("property")
        value = qualifier.get("value")
        if prop and value:
            line += f"|{prop}|{format_qs_value(value)}"
    return line


def parent_specs_for_item(
    item: Dict[str, Any],
    fallback_parent_qid: str,
    include_direct_parent: bool = True,
) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []

    # Parents the matched entity already has in Wikidata. For an orphan or
    # cross-listed match, an LLM-resolved joint parent may already be linked;
    # re-emitting it would add a duplicate QID|P749|existing_parent on upload, so
    # these are skipped during de-duplication below.
    existing_parents = set(split_qids(item.get("existing_parent_qids")))

    for parent in item.get("parents") or []:
        if isinstance(parent, dict):
            qid = clean_qid(parent.get("qid") or parent.get("parent_qid"))
            if qid:
                specs.append(
                    {
                        "qid": qid,
                        "qualifiers": parent.get("qualifiers") or [],
                    }
                )

    if include_direct_parent:
        direct_parent = clean_qid(item.get("parent_qid") or fallback_parent_qid)
        if direct_parent:
            specs.append({"qid": direct_parent, "qualifiers": item.get("qualifiers") or []})

    for field in ("parent_qids", "additional_parent_qids", "joint_parent_qids"):
        for qid in split_qids(item.get(field)):
            specs.append({"qid": qid, "qualifiers": item.get("joint_qualifiers") or []})

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for spec in specs:
        qid = clean_qid(spec.get("qid"))
        if not qid or qid in seen or qid in existing_parents:
            continue
        seen.add(qid)
        deduped.append({"qid": qid, "qualifiers": spec.get("qualifiers") or []})
    return deduped


def split_qids(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = re.split(r"[|,;]\s*", str(value))
    return [qid for qid in (clean_qid(v) for v in raw_values) if qid]


def normalize_status(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value == "exists_orphan":
        return "orphan"
    return value


def normalize_unit_type(unit_type: Any) -> str:
    value = str(unit_type or "").strip().lower()
    aliases = {
        "dept": "department",
        "academic department": "department",
        "college": "school",
        "faculty": "school",
        "laboratory": "lab",
        "centre": "center",
        "research centre": "center",
        "research center": "center",
    }
    return aliases.get(value, value or "unit")


def type_qid_for(unit_type: Any) -> str:
    normalized = normalize_unit_type(unit_type)
    return TYPE_MAP.get(normalized, TYPE_MAP[None])


def clean_qid(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text if re.fullmatch(r"Q\d+", text) else None


def format_qs_value(value: Any) -> str:
    text = str(value).strip()
    if re.fullmatch(r"Q\d+", text):
        return text
    if text.startswith("http://") or text.startswith("https://"):
        return f'"{escape_qs_string(text)}"'
    return f'"{escape_qs_string(text)}"'


def escape_qs_string(value: str) -> str:
    return value.replace('"', '\\"')


def normalize_website(value: Any) -> Optional[str]:
    """Return a schemed URL for a P856 statement, or None to omit it.

    P856 is optional, so a scheme-less but host-like value (e.g.
    "www.law.example.edu") is upgraded to https:// rather than dropped, and a
    value that cannot be a URL is omitted entirely instead of poisoning an
    otherwise valid CREATE/link block.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        # A scheme alone is not enough: reject values with whitespace or no
        # hostname (e.g. "https://not a url") so a malformed P856 is omitted.
        return text if is_valid_http_url(text) else None
    # Host-like: at least one dot, a TLD, no spaces; optional path/query.
    if re.fullmatch(r"[\w.-]+\.[A-Za-z]{2,}(/[^\s]*)?", text):
        return "https://" + text
    return None


def is_valid_http_url(text: str) -> bool:
    """True if text is a well-formed http(s) URL with a dotted hostname."""
    if not text or any(ch.isspace() for ch in text):
        return False
    parsed = urlparse(text)
    return bool(
        parsed.scheme in ("http", "https")
        and parsed.hostname
        and "." in parsed.hostname
    )
