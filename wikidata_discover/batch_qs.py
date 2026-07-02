"""Aggregate discovered units across universities into a single QuickStatements batch.

Collects every exportable unit (missing units to create, orphans to link,
cross-listed units needing extra joint parents), orders them by hierarchy level
so schools are created before departments, and emits one QuickStatements file.
Generated lines are also recorded in the BigQuery ``quickstatements_batches``
table (uploaded_at left null until they are actually submitted to Wikidata).
"""

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from wikidata_discover import bq_helpers
from wikidata_discover.config import console
from wikidata_discover.discovery import normalize_name
from wikidata_discover.to_qs_wikidata import build_quickstatements

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "results"


def bq_unit_to_export_row(unit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Adapt a ``discovered_units`` BigQuery row to a QuickStatements export row.

    Returns None for rows that produce no statements (e.g. plain exists_linked
    with no extra joint parents).
    """
    status = (unit.get("status") or "").strip()
    additional = (unit.get("additional_parent_qids") or "").strip()

    if status == "missing":
        export_status = "missing"
    elif status == "exists_orphan":
        export_status = "orphan"
    elif status == "exists_linked" and additional:
        export_status = "linked_joint"
    else:
        return None

    return {
        "name": unit.get("unit_name"),
        "unit_type": unit.get("unit_type"),
        "url": unit.get("website") or "",
        "location": unit.get("location") or "",
        "status": export_status,
        "qid": unit.get("matched_qid") or "",
        "parent_qid": unit.get("parent_qid"),
        "parent_label": unit.get("parent_label"),
        "additional_parent_qids": additional,
        # Carry the matched entity's existing parents so build_quickstatements
        # suppresses joint parents it is already linked to (avoids duplicate P749).
        "existing_parent_qids": unit.get("existing_parent_qids") or "",
        "level": unit.get("level"),
        "university_qid": unit.get("university_qid"),
        "university_label": unit.get("university_label"),
    }


def _level_key(row: Dict[str, Any]) -> tuple:
    """Sort key: hierarchy level ascending (schools first), then university QID."""
    level = row.get("level")
    try:
        level_int = int(level)
    except (TypeError, ValueError):
        level_int = 99
    return (level_int, str(row.get("university_qid") or ""))


def unit_key(row: Dict[str, Any]) -> str:
    """Stable identity for a discovered unit, used to diff against prior batches.

    CREATE units are keyed by (university, normalized name, parent); link units
    (orphan / cross-listed) by (matched QID, parent). The same unit rediscovered
    in a later run therefore produces the same key, so a diff can skip it.
    """
    status = str(row.get("status") or "").strip().lower()
    university = str(row.get("university_qid") or "")
    parent = str(row.get("parent_qid") or "")
    if status in ("orphan", "exists_orphan", "linked_joint"):
        qid = str(row.get("qid") or row.get("matched_qid") or "")
        return f"link|{qid}|{parent}"
    name = normalize_name(str(row.get("name") or row.get("unit_name") or ""))
    return f"create|{university}|{name}|{parent}"


def build_attributed_blocks(
    export_rows: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], List[str]]]:
    """Build the level-ordered QS batch as (source_row, block_lines) pairs.

    Rows are sorted by hierarchy level (schools before departments) and built one
    at a time so each block keeps its source row. Building per row is equivalent
    to one ``build_quickstatements`` call because rows are independent, and
    keeping the row lets validation drop a bad block and diff recording recover
    the university QID and unit key of the survivors.
    """
    ordered = sorted(export_rows, key=_level_key)
    blocks: List[Tuple[Dict[str, Any], List[str]]] = []
    for row in ordered:
        # Use the row's own university as the fallback parent so local CSVs that
        # carry university_qid but no parent_qid still emit LAST|P749|<university>.
        lines = build_quickstatements(
            [row],
            university_qid=str(row.get("university_qid") or ""),
            university_label=str(row.get("university_label") or ""),
        )
        if any(line.strip() for line in lines):
            blocks.append((row, lines))
    return blocks


def flatten_blocks(
    blocks: List[Tuple[Dict[str, Any], List[str]]],
) -> Tuple[List[str], List[Tuple[Optional[str], str, str]]]:
    """Flatten blocks into (all_lines, [(university_qid, unit_key, line), ...])."""
    all_lines: List[str] = []
    attributed: List[Tuple[Optional[str], str, str]] = []
    for row, block_lines in blocks:
        university_qid = row.get("university_qid")
        key = unit_key(row)
        for line in block_lines:
            all_lines.append(line)
            if line.strip():
                attributed.append((university_qid, key, line))
    return all_lines, attributed


def build_attributed_lines(
    export_rows: List[Dict[str, Any]],
) -> Tuple[List[str], List[Tuple[Optional[str], str, str]]]:
    """Build the level-ordered QS batch plus per-line (university, key) attribution."""
    return flatten_blocks(build_attributed_blocks(export_rows))


def filter_new_rows(
    export_rows: List[Dict[str, Any]],
    emitted_keys: set,
) -> List[Dict[str, Any]]:
    """Keep only rows whose unit_key was not already emitted in a prior batch."""
    return [row for row in export_rows if unit_key(row) not in emitted_keys]


def aggregate_quickstatements(export_rows: List[Dict[str, Any]]) -> List[str]:
    """Build a single level-ordered QuickStatements batch from export rows."""
    all_lines, _ = build_attributed_lines(export_rows)
    return all_lines


def load_export_rows(use_bq: bool = True) -> List[Dict[str, Any]]:
    """Load exportable units from BigQuery, falling back to local missing CSVs."""
    if use_bq:
        units = bq_helpers.try_get_exportable_units()
        if units is not None:
            rows = [bq_unit_to_export_row(u) for u in units]
            return [r for r in rows if r]
        console.print(
            "[yellow]BigQuery units unavailable; aggregating from local missing_divisions_*.csv.[/yellow]"
        )
    return load_local_export_rows()


def load_local_export_rows(
    directories: Optional[List[Path]] = None,
) -> List[Dict[str, Any]]:
    """Aggregate export rows from local ``missing_divisions_*.csv`` files.

    Scans the current working directory only by default, where ``discover``
    writes fresh per-university CSVs. The package ``results/`` directory is
    intentionally NOT scanned: it holds tracked historical CSVs that would
    otherwise pull stale, unrelated universities into a local batch. Pass an
    explicit ``directories`` list to aggregate from elsewhere.

    These CSVs are already written in export-row shape by discovery, so no status
    remapping is needed; only NaN cells are blanked for clean QS output.
    """
    search_dirs = directories if directories is not None else [Path.cwd()]
    rows: List[Dict[str, Any]] = []
    seen_paths = set()
    for directory in search_dirs:
        for csv_path in sorted(Path(directory).glob("missing_divisions_*.csv")):
            resolved = csv_path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                frame = pd.read_csv(csv_path).fillna("")
            except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
                logger.warning("Could not read %s: %s", csv_path, exc)
                continue
            rows.extend(frame.to_dict(orient="records"))
    return rows


def _validate_batch_blocks(
    blocks: List[Tuple[Dict[str, Any], List[str]]],
) -> List[Tuple[Dict[str, Any], List[str]]]:
    """Drop blocks that fail the ShEx schema, warning about each dropped unit."""
    from wikidata_discover.shex_validation import parse_qs_blocks, validate_block

    survivors: List[Tuple[Dict[str, Any], List[str]]] = []
    dropped = 0
    for row, block_lines in blocks:
        parsed = parse_qs_blocks(block_lines)
        violations = [v for block in parsed for v in validate_block(block)]
        if violations:
            dropped += 1
            describe = parsed[0].describe() if parsed else "(empty)"
            console.print(
                f"[yellow]ShEx dropped {describe} ({row.get('university_qid')}): "
                f"{'; '.join(violations)}[/yellow]"
            )
            continue
        survivors.append((row, block_lines))
    if dropped:
        console.print(
            f"[yellow]ShEx validation dropped {dropped} of {len(blocks)} block(s).[/yellow]"
        )
    return survivors


def quickstatements_batch_rows(
    batch_id: str,
    attributed: List[Tuple[Optional[str], str, str]],
) -> List[Dict[str, Any]]:
    """Shape attributed QS lines into ``quickstatements_batches`` rows.

    unit_key is recorded so a later diff run can skip units already emitted.
    uploaded_at is left null: these lines are generated, not yet submitted to
    Wikidata. The batch_id (stored in run_id) groups all lines of one batch.
    """
    return [
        {
            "run_id": batch_id,
            "university_qid": university_qid,
            "unit_key": key,
            "qs_line": line,
            "uploaded_at": None,
        }
        for university_qid, key, line in attributed
    ]


def generate_batch_quickstatements(
    use_bq: bool = True,
    out_path: Optional[Path] = None,
    validate: bool = True,
    diff: bool = False,
) -> Dict[str, Any]:
    """Generate a single aggregated QuickStatements batch across all universities.

    Returns a summary dict with the batch id, unit/line counts, and output path.
    When ``validate`` is True, blocks failing the ShEx schema are dropped before
    the file is written and recorded. When ``diff`` is True, units already
    emitted in a previous batch (recorded in ``quickstatements_batches``) are
    skipped so only genuinely new statements are produced.
    """
    batch_id = str(uuid.uuid4())
    export_rows = load_export_rows(use_bq=use_bq)

    skipped_existing = 0
    if diff and use_bq:
        emitted_keys = bq_helpers.try_get_emitted_unit_keys()
        before = len(export_rows)
        export_rows = filter_new_rows(export_rows, emitted_keys)
        skipped_existing = before - len(export_rows)
        console.print(
            f"[dim]Diff mode: skipped {skipped_existing} unit(s) already emitted "
            f"in a previous batch.[/dim]"
        )
    elif diff and not use_bq:
        console.print(
            "[yellow]Diff mode needs BigQuery to read prior batches; "
            "ignoring --diff under --no-bq.[/yellow]"
        )

    blocks = build_attributed_blocks(export_rows)

    if validate:
        blocks = _validate_batch_blocks(blocks)
    all_lines, attributed = flatten_blocks(blocks)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = out_path or (RESULTS_DIR / f"quickstatements_batch_{batch_id}.qs")
    path.write_text("\n".join(all_lines))
    console.print(
        f"[green]Aggregated {len(export_rows)} units into {len(all_lines)} "
        f"QuickStatements lines: {path}[/green]"
    )

    saved_to_bq = False
    if use_bq and attributed:
        bq_rows = quickstatements_batch_rows(batch_id, attributed)
        saved_to_bq = bq_helpers.try_save_quickstatements_batch(bq_rows)
        if saved_to_bq:
            console.print(
                f"[green]Recorded {len(bq_rows)} QuickStatements lines in BigQuery "
                f"(batch {batch_id}).[/green]"
            )
        else:
            console.print("[yellow]BigQuery unavailable; kept local .qs file only.[/yellow]")

    return {
        "batch_id": batch_id,
        "units": len(export_rows),
        "lines": len(all_lines),
        "skipped_existing": skipped_existing,
        "path": str(path),
        "saved_to_bq": saved_to_bq,
    }
