import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

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
        ("qs_line", "STRING", "NULLABLE"),
        ("uploaded_at", "TIMESTAMP", "NULLABLE"),
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
        try:
            client.get_table(table_ref)
        except NotFound:
            table = bq.Table(table_ref, schema=_schema(fields))
            client.create_table(table)
            logger.info("Created BigQuery table %s", table_ref)


def _insert_rows(table_name: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_dataset_and_tables()
    client = _client()
    errors = client.insert_rows_json(_table_id(table_name), rows)
    if errors:
        raise RuntimeError(f"BigQuery insert into {table_name} failed: {errors}")


def save_universities(rows: List[Dict[str, Any]]) -> None:
    _insert_rows("universities", rows)


def save_discovery_run(row: Dict[str, Any]) -> None:
    _insert_rows("discovery_runs", [row])


def save_discovered_units(rows: List[Dict[str, Any]]) -> None:
    _insert_rows("discovered_units", rows)


def get_processed_qids() -> set[str]:
    ensure_dataset_and_tables()
    client = _client()
    query = f"""
    SELECT DISTINCT university_qid
    FROM `{_table_id("discovery_runs")}`
    WHERE university_qid IS NOT NULL
    """
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
