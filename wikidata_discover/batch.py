"""Batch discovery: run the discover pipeline across many universities.

Reads the university list from BigQuery (``universities`` table) when available,
falling back to the local ``universities_us.json`` harvest file. Supports resume
by skipping QIDs already recorded in ``discovery_runs``, per-university error
isolation, and a JSON summary report written to ``results/batch_reports/``.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wikidata_discover import bq_helpers
from wikidata_discover.config import console
from wikidata_discover.discovery import Discovery

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "results"
# The harvester writes ``universities_us.json`` to the current working directory;
# an older copy also lives under results/. Check both when falling back to local.
LOCAL_UNIVERSITY_PATHS = (
    Path("universities_us.json"),
    RESULTS_DIR / "universities_us.json",
)


def parse_universities_payload(data: Any) -> List[Tuple[str, Optional[str]]]:
    """Parse a harvested universities JSON payload into (qid, label) pairs.

    Handles both formats the project has produced:
      - list of ``[qid, label]`` pairs (current ``run_sparql(as_tuples=True)``)
      - list of raw SPARQL bindings with a university URI + label
    Rows without a resolvable QID are skipped.
    """
    pairs: List[Tuple[str, Optional[str]]] = []
    if not isinstance(data, list):
        return pairs

    for row in data:
        qid: Optional[str] = None
        label: Optional[str] = None

        if isinstance(row, (list, tuple)):
            if row:
                qid = _qid_from_value(row[0])
                label = row[1] if len(row) > 1 else None
        elif isinstance(row, dict):
            # Support both harvest key conventions (univ/univLabel and
            # university/universityLabel) seen across older and newer runs.
            for key in ("univ", "university", "qid"):
                if key in row:
                    qid = _qid_from_value(_binding_value(row[key]))
                    break
            for key in ("univLabel", "universityLabel", "label"):
                if key in row:
                    label = _binding_value(row[key])
                    break

        if qid:
            # A label identical to the QID is a missing-label placeholder.
            if label == qid:
                label = None
            pairs.append((qid, label))
    return pairs


def _binding_value(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("value")
    return value


def _qid_from_value(value: Any) -> Optional[str]:
    text = _binding_value(value)
    if not text:
        return None
    text = str(text).strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text or None


def load_local_universities() -> List[Tuple[str, Optional[str]]]:
    """Load the university list from the first available local harvest file."""
    for path in LOCAL_UNIVERSITY_PATHS:
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read local universities file %s: %s", path, exc)
                continue
            pairs = parse_universities_payload(data)
            if pairs:
                logger.info("Loaded %d universities from %s", len(pairs), path)
                return pairs
    return []


def load_universities(use_bq: bool = True) -> List[Tuple[str, Optional[str]]]:
    """Load universities from BigQuery when available, else the local harvest file."""
    if use_bq:
        rows = bq_helpers.try_get_universities()
        if rows:
            return [(row["qid"], row.get("label")) for row in rows if row.get("qid")]
        console.print(
            "[yellow]BigQuery universities unavailable; falling back to local harvest file.[/yellow]"
        )
    return load_local_universities()


def select_pending(
    universities: List[Tuple[str, Optional[str]]],
    processed_qids: set,
    limit: Optional[int] = None,
) -> List[Tuple[str, Optional[str]]]:
    """Filter out already-processed QIDs (dedupe kept order) and apply an optional limit."""
    pending: List[Tuple[str, Optional[str]]] = []
    seen: set = set()
    for qid, label in universities:
        if not qid or qid in processed_qids or qid in seen:
            continue
        seen.add(qid)
        pending.append((qid, label))
    if limit is not None and limit >= 0:
        pending = pending[:limit]
    return pending


def run_batch_discovery(
    depth: int = 1,
    write_bq: bool = True,
    limit: Optional[int] = None,
    resume: bool = True,
) -> Dict[str, Any]:
    """Discover divisions for every harvested university.

    Args:
        depth: Hierarchy depth passed to ``Discovery.discover_missing``.
        write_bq: When False, skip all BigQuery reads and writes (local files only).
        limit: Optional cap on how many universities to process this run.
        resume: When True, skip QIDs already present in ``discovery_runs``.

    Returns a summary dict (also written to ``results/batch_reports/``).
    """
    universities = load_universities(use_bq=write_bq)
    if not universities:
        console.print(
            "[red]No universities found. Run 'harvest' first or provide QIDs directly.[/red]"
        )
        return _empty_summary(depth, limit, resume)

    processed_qids = (
        bq_helpers.try_get_processed_qids() if (resume and write_bq) else set()
    )
    pending = select_pending(universities, processed_qids, limit=limit)

    total = len(pending)
    console.print(
        f"[bold]Batch discovery:[/bold] {total} universities to process "
        f"({len(processed_qids)} already done, {len(universities)} total known)."
    )

    succeeded: List[str] = []
    failed: List[Dict[str, str]] = []
    total_missing = 0

    for index, (qid, label) in enumerate(pending, start=1):
        console.print(
            f"[cyan][{index}/{total}][/cyan] Discovering {label or qid} ({qid})"
        )
        try:
            missing = Discovery(qid).discover_missing(depth=depth, write_bq=write_bq)
            total_missing += len(missing)
            succeeded.append(qid)
        except Exception as exc:  # isolate one university's failure from the batch
            logger.exception("Batch discovery failed for %s", qid)
            console.print(f"[red]Failed {qid}: {exc}[/red]")
            failed.append({"qid": qid, "label": label or "", "error": str(exc)})

    summary = {
        "depth": depth,
        "resume": resume,
        "limit": limit,
        "total_known": len(universities),
        "already_processed": len(processed_qids),
        "attempted": total,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "total_missing": total_missing,
        "failures": failed,
        "timestamp": bq_helpers.utc_now_iso(),
    }
    _write_summary(summary)
    console.print(
        f"[green]Batch complete:[/green] {len(succeeded)} succeeded, "
        f"{len(failed)} failed, {total_missing} missing/orphan units found."
    )
    return summary


def _empty_summary(depth: int, limit: Optional[int], resume: bool) -> Dict[str, Any]:
    return {
        "depth": depth,
        "resume": resume,
        "limit": limit,
        "total_known": 0,
        "already_processed": 0,
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "total_missing": 0,
        "failures": [],
        "timestamp": bq_helpers.utc_now_iso(),
    }


def _write_summary(summary: Dict[str, Any]) -> Path:
    reports_dir = RESULTS_DIR / "batch_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary["timestamp"].replace(":", "").replace("-", "").replace(".", "")
    path = reports_dir / f"batch_{stamp}.json"
    path.write_text(json.dumps(summary, indent=2))
    console.print(f"[dim]Batch summary written to {path}[/dim]")
    return path
