import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from wikidata_discover.config import BQ_DATASET, GCP_PROJECT

logger = logging.getLogger(__name__)

try:
    from google.cloud import bigquery
    from google.api_core.exceptions import NotFound
except ImportError:  # pragma: no cover - exercised in environments without GCP deps
    bigquery = None
    NotFound = Exception


TABLE_SCHEMAS = {
    "universities": [
        ("qid", "STRING", "REQUIRED"),
        ("label", "STRING", "NULLABLE"),
        ("website", "STRING", "NULLABLE"),
        ("country", "STRING", "NULLABLE"),
        ("ipeds_id", "STRING", "NULLABLE"),
        ("identifier", "STRING", "NULLABLE"),
        ("identifier_property", "STRING", "NULLABLE"),
        ("harvested_at", "TIMESTAMP", "NULLABLE"),
    ],
    "discovery_runs": [
        ("run_id", "STRING", "REQUIRED"),
        ("university_qid", "STRING", "NULLABLE"),
        ("university_label", "STRING", "NULLABLE"),
        ("model", "STRING", "NULLABLE"),
        ("timestamp", "TIMESTAMP", "NULLABLE"),
        ("depth", "INTEGER", "NULLABLE"),
        ("total_candidates", "INTEGER", "NULLABLE"),
        ("exists_linked", "INTEGER", "NULLABLE"),
        ("exists_orphan", "INTEGER", "NULLABLE"),
        ("missing", "INTEGER", "NULLABLE"),
    ],
    "discovered_units": [
        ("run_id", "STRING", "REQUIRED"),
        ("university_qid", "STRING", "NULLABLE"),
        ("university_label", "STRING", "NULLABLE"),
        ("unit_name", "STRING", "NULLABLE"),
        ("unit_type", "STRING", "NULLABLE"),
        ("status", "STRING", "NULLABLE"),
        ("matched_qid", "STRING", "NULLABLE"),
        ("website", "STRING", "NULLABLE"),
        ("location", "STRING", "NULLABLE"),
        ("parent_qid", "STRING", "NULLABLE"),
        ("parent_label", "STRING", "NULLABLE"),
        ("is_joint", "BOOLEAN", "NULLABLE"),
        ("parent_names", "STRING", "NULLABLE"),
        ("additional_parent_qids", "STRING", "NULLABLE"),
        ("existing_parent_qids", "STRING", "NULLABLE"),
        ("unresolved_parent_names", "STRING", "NULLABLE"),
        ("evidence", "STRING", "NULLABLE"),
        ("level", "INTEGER", "NULLABLE"),
        ("path", "STRING", "NULLABLE"),
        ("reference", "STRING", "NULLABLE"),
        ("discovered_at", "TIMESTAMP", "NULLABLE"),
    ],
    "quickstatements_batches": [
        ("run_id", "STRING", "REQUIRED"),
        ("university_qid", "STRING", "NULLABLE"),
        ("unit_key", "STRING", "NULLABLE"),
        ("qs_line", "STRING", "NULLABLE"),
        ("uploaded_at", "TIMESTAMP", "NULLABLE"),
    ],
    "ipeds_reconciliation": [
        ("ipeds_id", "STRING", "REQUIRED"),
        ("name", "STRING", "NULLABLE"),
        ("website", "STRING", "NULLABLE"),
        ("matched_qid", "STRING", "NULLABLE"),
        ("status", "STRING", "NULLABLE"),
        ("reconciled_at", "TIMESTAMP", "NULLABLE"),
    ],
}


def _require_bigquery():
    if bigquery is None:
        raise RuntimeError(
            "google-cloud-bigquery is not installed. Install requirements or use --no-bq."
        )
    return bigquery


def _client():
    bq = _require_bigquery()
    return bq.Client(project=GCP_PROJECT)


def _dataset_id() -> str:
    return f"{GCP_PROJECT}.{BQ_DATASET}"


def _table_id(table_name: str) -> str:
    return f"{_dataset_id()}.{table_name}"


def _schema(fields: Iterable[tuple[str, str, str]]) -> list:
    bq = _require_bigquery()
    return [bq.SchemaField(name, field_type, mode=mode) for name, field_type, mode in fields]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dataset_and_tables() -> None:
    """Create the configured dataset and Step 1 tables if they do not exist."""
    bq = _require_bigquery()
    client = _client()

    dataset_ref = bigquery.Dataset(_dataset_id())
    try:
        client.get_dataset(dataset_ref)
    except NotFound:
        dataset_ref.location = "US"
        client.create_dataset(dataset_ref)
        logger.info("Created BigQuery dataset %s", _dataset_id())

    for table_name, fields in TABLE_SCHEMAS.items():
        table_ref = _table_id(table_name)
        desired = _schema(fields)
        try:
            table = client.get_table(table_ref)
        except NotFound:
            table = bq.Table(table_ref, schema=desired)
            client.create_table(table)
            logger.info("Created BigQuery table %s", table_ref)
            continue

        # Reconcile: add any newly-defined NULLABLE columns to an existing table
        # so inserts including new fields do not fail. BigQuery only allows
        # adding NULLABLE/REPEATED columns, which is all this schema introduces.
        existing_names = {field.name for field in table.schema}
        additions = [field for field in desired if field.name not in existing_names]
        if additions:
            table.schema = list(table.schema) + additions
            client.update_table(table, ["schema"])
            logger.info(
                "Added %d column(s) to %s: %s",
                len(additions), table_ref, [field.name for field in additions],
            )


# BigQuery streaming inserts are capped (~50k rows / ~10 MB per request), so a
# full-university batch must be chunked or the whole insert fails.
_INSERT_CHUNK_SIZE = 500


def _insert_rows(table_name: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_dataset_and_tables()
    client = _client()
    table_id = _table_id(table_name)
    for start in range(0, len(rows), _INSERT_CHUNK_SIZE):
        chunk = rows[start:start + _INSERT_CHUNK_SIZE]
        errors = client.insert_rows_json(table_id, chunk)
        if errors:
            raise RuntimeError(
                f"BigQuery insert into {table_name} failed "
                f"(rows {start}-{start + len(chunk)}): {errors}"
            )


def save_universities(rows: List[Dict[str, Any]]) -> None:
    _insert_rows("universities", rows)


def save_discovery_run(row: Dict[str, Any]) -> None:
    _insert_rows("discovery_runs", [row])


def save_discovered_units(rows: List[Dict[str, Any]]) -> None:
    _insert_rows("discovered_units", rows)


def get_universities() -> List[Dict[str, Any]]:
    """Return the harvested universities (deduped by QID) from BigQuery."""
    ensure_dataset_and_tables()
    client = _client()
    query = f"""
    SELECT
      qid,
      ANY_VALUE(label) AS label,
      ANY_VALUE(website) AS website,
      ANY_VALUE(country) AS country,
      ANY_VALUE(ipeds_id) AS ipeds_id
    FROM `{_table_id("universities")}`
    WHERE qid IS NOT NULL
    GROUP BY qid
    ORDER BY qid
    """
    return [dict(row.items()) for row in client.query(query).result()]


def get_exportable_units() -> List[Dict[str, Any]]:
    """Return exportable discovered units from the latest run per university.

    Only rows that yield QuickStatements are returned: missing units (to create),
    orphans (to link), and already-linked units that are cross-listed and still
    need their remaining joint-parent links. Restricting to the latest run per
    university avoids re-emitting CREATE lines from earlier re-runs. Ordered by
    hierarchy level so schools come before departments in the aggregated batch.
    """
    ensure_dataset_and_tables()
    client = _client()
    query = f"""
    WITH latest AS (
      SELECT
        university_qid,
        run_id,
        ROW_NUMBER() OVER (
          PARTITION BY university_qid ORDER BY timestamp DESC
        ) AS rn
      FROM `{_table_id("discovery_runs")}`
    )
    SELECT u.*
    FROM `{_table_id("discovered_units")}` AS u
    JOIN latest AS l
      ON u.run_id = l.run_id AND l.rn = 1
    WHERE u.status IN ('missing', 'exists_orphan')
       OR (
         u.status = 'exists_linked'
         AND u.additional_parent_qids IS NOT NULL
         AND u.additional_parent_qids != ''
       )
    ORDER BY u.level, u.university_qid
    """
    return [dict(row.items()) for row in client.query(query).result()]


def save_quickstatements_batch(rows: List[Dict[str, Any]]) -> None:
    _insert_rows("quickstatements_batches", rows)


def save_ipeds_reconciliation(rows: List[Dict[str, Any]]) -> None:
    _insert_rows("ipeds_reconciliation", rows)


def try_save_ipeds_reconciliation(rows: List[Dict[str, Any]]) -> bool:
    return _try_save(lambda: save_ipeds_reconciliation(rows), "ipeds reconciliation")


def get_processed_qids(min_depth: Optional[int] = None) -> set[str]:
    """Return QIDs already discovered.

    When ``min_depth`` is given, only QIDs whose deepest prior run reached at
    least that depth are considered processed. This lets batch resume re-run a
    university that was previously discovered shallower than the depth now
    requested (e.g. a depth-1 run must not skip a later ``--depth 3`` batch).
    Runs predating the depth column (NULL depth) are treated as depth 1.
    """
    ensure_dataset_and_tables()
    client = _client()
    query = f"""
    SELECT DISTINCT university_qid
    FROM `{_table_id("discovery_runs")}`
    WHERE university_qid IS NOT NULL
    """
    if min_depth is not None:
        query += f"      AND COALESCE(depth, 1) >= {int(min_depth)}\n"
    return {
        row.university_qid
        for row in client.query(query).result()
        if row.university_qid
    }


def get_coverage_summary() -> List[Dict[str, Any]]:
    ensure_dataset_and_tables()
    client = _client()
    query = f"""
    SELECT
      university_qid,
      ANY_VALUE(university_label) AS university_label,
      COUNT(*) AS run_count,
      MAX(timestamp) AS last_run_at,
      SUM(total_candidates) AS total_candidates,
      SUM(exists_linked) AS exists_linked,
      SUM(exists_orphan) AS exists_orphan,
      SUM(missing) AS missing
    FROM `{_table_id("discovery_runs")}`
    GROUP BY university_qid
    ORDER BY last_run_at DESC
    """
    return [dict(row.items()) for row in client.query(query).result()]


def try_get_universities() -> Optional[List[Dict[str, Any]]]:
    """Best-effort read of the universities table; None if BigQuery is unavailable."""
    try:
        return get_universities()
    except Exception as exc:
        logger.warning("Could not read universities from BigQuery: %s", exc)
        return None


def try_get_exportable_units() -> Optional[List[Dict[str, Any]]]:
    """Best-effort read of exportable units; None if BigQuery is unavailable."""
    try:
        return get_exportable_units()
    except Exception as exc:
        logger.warning("Could not read exportable units from BigQuery: %s", exc)
        return None


def try_save_quickstatements_batch(rows: List[Dict[str, Any]]) -> bool:
    return _try_save(lambda: save_quickstatements_batch(rows), "quickstatements batch")


def get_emitted_unit_keys() -> set[str]:
    """Return the set of unit_keys already emitted in previous QS batches."""
    ensure_dataset_and_tables()
    client = _client()
    query = f"""
    SELECT DISTINCT unit_key
    FROM `{_table_id("quickstatements_batches")}`
    WHERE unit_key IS NOT NULL
    """
    return {row.unit_key for row in client.query(query).result() if row.unit_key}


def try_get_emitted_unit_keys() -> set[str]:
    """Best-effort read of previously-emitted unit keys; empty set on failure."""
    try:
        return get_emitted_unit_keys()
    except Exception as exc:
        logger.warning("Could not read emitted unit keys from BigQuery: %s", exc)
        return set()


def try_get_processed_qids(min_depth: Optional[int] = None) -> set[str]:
    """Best-effort read of already-processed QIDs; empty set if BigQuery is unavailable."""
    try:
        return get_processed_qids(min_depth=min_depth)
    except Exception as exc:
        logger.warning("Could not read processed QIDs from BigQuery: %s", exc)
        return set()


def try_save_universities(rows: List[Dict[str, Any]]) -> bool:
    return _try_save(lambda: save_universities(rows), "universities")


def try_save_discovery_run(row: Dict[str, Any]) -> bool:
    return _try_save(lambda: save_discovery_run(row), "discovery run")


def try_save_discovered_units(rows: List[Dict[str, Any]]) -> bool:
    return _try_save(lambda: save_discovered_units(rows), "discovered units")


def _try_save(operation, label: str) -> bool:
    try:
        operation()
        return True
    except Exception as exc:
        logger.warning("Skipping BigQuery write for %s: %s", label, exc)
        return False
