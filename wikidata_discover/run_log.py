"""Run log: a record of every run in BigQuery, with large text in Cloud Storage.

Four tables in dataset `academiabot` of project `wikidata-academia` (see "Run log" in
AGENTS.md): runs, llm_calls, evidence, candidates. Full prompts and raw responses go
to gs://academiabot/runs/<run_id>/llm_calls/<llm_call_id>/ and the row keeps the path.

When BigQuery or the bucket cannot be reached, the rows and texts are written as JSON
under results/runs/<run_id>/runlog/<invocation_id>/ instead, so nothing is lost. A
failure is sticky for the rest of the invocation: one unreachable service costs one
timeout, not one per call.

Usage: start() at the beginning of a command, flush() after each university, finish()
at the end. Code that makes LLM calls or proposes candidates calls current() and does
nothing when no run log is active (tests, the eval harness).

One-time setup of the dataset and tables:
    python -m wikidata_discover.run_log setup
"""
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from wikidata_discover import config

logger = logging.getLogger(__name__)

TABLES = ("runs", "llm_calls", "evidence", "candidates")

# Column name, BigQuery type, mode. JSON-shaped values (args, config, request_config)
# are stored as JSON text in STRING columns.
SCHEMAS: Dict[str, List[tuple]] = {
    "runs": [
        ("run_id", "STRING", "REQUIRED"), ("invocation_id", "STRING", "REQUIRED"),
        ("who", "STRING", "NULLABLE"), ("git_commit", "STRING", "NULLABLE"),
        ("command", "STRING", "NULLABLE"), ("args", "STRING", "NULLABLE"),
        ("qids", "STRING", "REPEATED"), ("config", "STRING", "NULLABLE"),
        ("host", "STRING", "NULLABLE"),
        ("started_at", "TIMESTAMP", "NULLABLE"), ("ended_at", "TIMESTAMP", "NULLABLE"),
        ("outcome", "STRING", "NULLABLE"), ("summary", "STRING", "NULLABLE"),
        ("source_patch_path", "STRING", "NULLABLE"), ("storage_problems", "STRING", "NULLABLE"),
    ],
    "llm_calls": [
        ("llm_call_id", "STRING", "REQUIRED"), ("run_id", "STRING", "REQUIRED"),
        ("invocation_id", "STRING", "REQUIRED"), ("university_qid", "STRING", "NULLABLE"),
        ("provider", "STRING", "NULLABLE"), ("model", "STRING", "NULLABLE"),
        ("purpose", "STRING", "NULLABLE"), ("attempt", "INTEGER", "NULLABLE"),
        ("request_config", "STRING", "NULLABLE"), ("cache_key", "STRING", "NULLABLE"),
        ("prompt_hash", "STRING", "NULLABLE"), ("prompt_path", "STRING", "NULLABLE"),
        ("response_path", "STRING", "NULLABLE"), ("raw_response_available", "BOOLEAN", "NULLABLE"),
        ("cache_hit", "BOOLEAN", "NULLABLE"), ("cached_from_llm_call_id", "STRING", "NULLABLE"),
        ("tokens_in", "INTEGER", "NULLABLE"), ("tokens_out", "INTEGER", "NULLABLE"),
        ("latency_ms", "INTEGER", "NULLABLE"), ("error", "STRING", "NULLABLE"),
        ("note", "STRING", "NULLABLE"), ("called_at", "TIMESTAMP", "NULLABLE"),
        ("legacy", "BOOLEAN", "NULLABLE"), ("source_file", "STRING", "NULLABLE"),
    ],
    "evidence": [
        ("evidence_id", "STRING", "REQUIRED"), ("run_id", "STRING", "REQUIRED"),
        ("invocation_id", "STRING", "REQUIRED"), ("url", "STRING", "NULLABLE"),
        ("fetched_at", "TIMESTAMP", "NULLABLE"), ("http_status", "INTEGER", "NULLABLE"),
        ("content_hash", "STRING", "NULLABLE"), ("snapshot_path", "STRING", "NULLABLE"),
        ("unit_names", "STRING", "REPEATED"),
    ],
    "candidates": [
        ("candidate_id", "STRING", "REQUIRED"), ("run_id", "STRING", "REQUIRED"),
        ("invocation_id", "STRING", "REQUIRED"), ("university_qid", "STRING", "NULLABLE"),
        ("parent_qid", "STRING", "NULLABLE"), ("parent_candidate_id", "STRING", "NULLABLE"),
        ("name", "STRING", "NULLABLE"), ("unit_type", "STRING", "NULLABLE"),
        ("status", "STRING", "NULLABLE"), ("matched_qid", "STRING", "NULLABLE"),
        ("matched_label", "STRING", "NULLABLE"), ("match_method", "STRING", "NULLABLE"),
        ("match_choices", "STRING", "NULLABLE"), ("source_url", "STRING", "NULLABLE"),
        ("website", "STRING", "NULLABLE"), ("provider", "STRING", "NULLABLE"),
        ("llm_call_ids", "STRING", "REPEATED"), ("evidence_ids", "STRING", "REPEATED"),
        ("created_at", "TIMESTAMP", "NULLABLE"),
        ("legacy", "BOOLEAN", "NULLABLE"), ("source_file", "STRING", "NULLABLE"),
    ],
}

CANDIDATE_STATUSES = ("linked", "orphan", "missing", "unresolved")

# Columns added after the tables were first created (week 3). setup_tables() adds them
# to existing tables, and fills `legacy` with false on rows written before.
ADDED_COLUMNS = {"llm_calls": ("legacy", "source_file"), "candidates": ("legacy", "source_file")}

# Written on a cache-hit row whose answer has no saved raw response.
NO_RAW_RESPONSE_NOTE = ("No raw response exists: this answer was cached before the run log "
                        "started (2026-10-14) or by a run without a run log. Only the parsed "
                        "answer in results/cache was kept.")

_GCS_TIMEOUT_S = 30
_BQ_TIMEOUT_S = 60

# When True, start() returns a log that records nothing. The test suite sets it so
# that no test writes to the real BigQuery or bucket by accident.
DISABLED = False

_current: Optional["RunLog"] = None


def current() -> Optional["RunLog"]:
    """The active run log, or None."""
    return _current


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id() -> str:
    return uuid.uuid4().hex


def new_run_id(command: str) -> str:
    """A run id for a terminal command: <command>-<UTC time>-<6 hex>. Safe as one path
    component and a bucket prefix."""
    return f"{command}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"


def bigquery_schema(table: str):
    from google.cloud import bigquery
    return [bigquery.SchemaField(n, t, mode=m) for n, t, m in SCHEMAS[table]]


def table_ref(table: str) -> str:
    return f"{config.GCP_PROJECT}.{config.RUNLOG_DATASET}.{table}"


def setup_tables(client=None) -> List[str]:
    """Create the dataset and the four tables if they do not exist. Returns the table ids.
    Run once by a person; runs never create tables, so a wrong dataset name fails
    loudly (and falls back to local files) instead of silently making a new dataset."""
    from google.cloud import bigquery
    client = client or bigquery.Client(project=config.GCP_PROJECT)
    ds = bigquery.Dataset(f"{config.GCP_PROJECT}.{config.RUNLOG_DATASET}")
    ds.location = "US"
    client.create_dataset(ds, exists_ok=True)
    made = []
    for t in TABLES:
        table = client.create_table(bigquery.Table(table_ref(t), schema=bigquery_schema(t)), exists_ok=True)
        add_missing_columns(client, table, t)
        made.append(table_ref(t))
    return made


def missing_columns(table: str, existing_names) -> list:
    """Schema columns of `table` that the BigQuery table does not have yet. Pure."""
    have = set(existing_names)
    return [c for c in SCHEMAS[table] if c[0] not in have]


def add_missing_columns(client, table, name: str) -> list:
    """Add new NULLABLE columns to an existing table, then set legacy = false on the
    rows that were written before the column existed. Returns the added names."""
    from google.cloud import bigquery
    new = missing_columns(name, [f.name for f in table.schema])
    if new:
        table.schema = list(table.schema) + [bigquery.SchemaField(n, ty, mode=m) for n, ty, m in new]
        client.update_table(table, ["schema"])
    if "legacy" in ADDED_COLUMNS.get(name, ()):
        client.query(f"UPDATE `{table_ref(name)}` SET legacy = FALSE WHERE legacy IS NULL").result()
    return [n for n, _, _ in new]


def _bucket_timeout() -> float:
    """Timeout for one bucket write: 30 s, less near the kill time. Raises when no
    write may start (the batch runner keeps the last seconds for its own records)."""
    from wikidata_discover.batch import upload_timeout
    return min(_GCS_TIMEOUT_S, upload_timeout())


def clean_row(table: str, row: dict) -> dict:
    """Keep only the schema columns, turn dict and list values of STRING columns into
    JSON text, and fill missing REPEATED columns with []. Pure function."""
    out = {}
    for name, typ, mode in SCHEMAS[table]:
        v = row.get(name)
        if mode == "REPEATED":
            v = [str(x) for x in (v or [])]
        elif typ == "STRING" and isinstance(v, (dict, list)):
            v = json.dumps(v, sort_keys=True, default=str)
        out[name] = v
    return out


class RunLog:
    """Rows and texts for one invocation of a command."""

    def __init__(self, run_id: str, invocation_id: Optional[str] = None, *, command: str = "",
                 args: Any = None, qids=(), who: str = "", git_commit: str = "",
                 run_config: Optional[dict] = None, bucket=None, bq_client=None,
                 results_dir: Optional[Path] = None, enabled: bool = True):
        self.run_id = run_id
        self.invocation_id = invocation_id or uuid.uuid4().hex[:12]
        self.enabled = enabled
        self.results_dir = results_dir or config.RESULTS_DIR
        self.local_dir = self.results_dir / "runs" / run_id / "runlog" / self.invocation_id
        self.university_qid: Optional[str] = None
        self._bucket = bucket
        self._bq = bq_client
        self.gcs_error: Optional[str] = None
        self.bq_error: Optional[str] = None
        self.local_written = False
        self.pending: Dict[str, List[dict]] = {t: [] for t in TABLES}
        self.counts: Dict[str, int] = {t: 0 for t in TABLES}
        self.text_paths: Dict[str, dict] = {}   # llm_call_id -> {prompt_path, response_path}
        self.run_row = {"run_id": run_id, "invocation_id": self.invocation_id, "who": who,
                        "git_commit": git_commit, "command": command, "args": args,
                        "qids": list(qids), "config": run_config or {},
                        "host": os.getenv("K_SERVICE", "local"), "started_at": now_iso()}

    # ── storage ──────────────────────────────────────────────────────────

    def _get_bucket(self):
        if self._bucket is None:
            from google.cloud import storage
            self._bucket = storage.Client(project=config.GCP_PROJECT).bucket(config.RUNLOG_BUCKET)
        return self._bucket

    def _get_bq(self):
        if self._bq is None:
            from google.cloud import bigquery
            self._bq = bigquery.Client(project=config.GCP_PROJECT)
        return self._bq

    def bucket_name(self) -> str:
        return getattr(self._bucket, "name", None) or config.RUNLOG_BUCKET

    def save_text(self, rel_path: str, text: str, content_type: str = "text/plain") -> str:
        """Store one text under the run folder. Returns gs://... when the bucket took it,
        else the local file path. Called before the text is parsed, so a parse error
        never loses it."""
        if not self.gcs_error:
            name = f"runs/{self.run_id}/{rel_path}"
            try:
                self._get_bucket().blob(name).upload_from_string(
                    text, content_type=content_type, timeout=_bucket_timeout())
                return f"gs://{self.bucket_name()}/{name}"
            except Exception as e:  # noqa: BLE001 - fall back to a local file
                self.gcs_error = f"{type(e).__name__}: {str(e)[:200]}"
                logger.warning("run log: bucket write failed, keeping texts locally from now on (%s)", self.gcs_error)
        path = self.local_dir / "text" / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.local_written = True
        return str(path)

    # ── rows ─────────────────────────────────────────────────────────────

    def set_university(self, qid: Optional[str]) -> None:
        self.university_qid = qid

    def _add(self, table: str, row: dict) -> None:
        row = {"run_id": self.run_id, "invocation_id": self.invocation_id, **row}
        self.pending[table].append(clean_row(table, row))
        self.counts[table] += 1

    def record_llm_call(self, *, provider: str, model: str, purpose: str, prompt: Any,
                        raw_response: Optional[str] = None, request_config: Optional[dict] = None,
                        cache_key: Optional[str] = None, prompt_hash: Optional[str] = None,
                        attempt: Optional[int] = None, tokens_in: Optional[int] = None,
                        tokens_out: Optional[int] = None, latency_ms: Optional[int] = None,
                        cache_hit: bool = False, cached_from: Optional[dict] = None,
                        error: Optional[str] = None) -> str:
        """Add one llm_calls row and store its prompt and raw response. Returns its id.

        For a cache hit, cached_from is the record the original call left next to the
        cache file ({llm_call_id, prompt_path, response_path}); without it there is no
        raw response, and the row says so."""
        call_id = new_id()
        base = f"llm_calls/{call_id}"
        prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt, indent=2, ensure_ascii=False)
        row = {"llm_call_id": call_id, "university_qid": self.university_qid, "provider": provider,
               "model": model, "purpose": purpose, "attempt": attempt,
               "request_config": request_config or {}, "cache_key": cache_key, "prompt_hash": prompt_hash,
               "tokens_in": tokens_in, "tokens_out": tokens_out, "latency_ms": latency_ms,
               "cache_hit": cache_hit, "error": error, "called_at": now_iso(), "legacy": False}
        if cache_hit:
            cached_from = cached_from or {}
            row["cached_from_llm_call_id"] = cached_from.get("llm_call_id")
            row["prompt_path"] = cached_from.get("prompt_path") or self.save_text(f"{base}/prompt.json", prompt_text, "application/json")
            row["response_path"] = cached_from.get("response_path")
            row["raw_response_available"] = bool(row["response_path"])
            if not row["response_path"]:
                row["note"] = NO_RAW_RESPONSE_NOTE
        else:
            row["prompt_path"] = self.save_text(f"{base}/prompt.json", prompt_text, "application/json")
            if raw_response is not None:
                row["response_path"] = self.save_text(f"{base}/response.txt", raw_response)
            row["raw_response_available"] = raw_response is not None
            if raw_response is None:
                row["note"] = "The request failed before any response arrived." if error else "Empty response."
        self.text_paths[call_id] = {"prompt_path": row["prompt_path"], "response_path": row.get("response_path")}
        self._add("llm_calls", row)
        return call_id

    def add_candidate(self, **fields) -> str:
        """Add one candidates row. status must be linked, orphan, missing, or unresolved."""
        if fields.get("status") not in CANDIDATE_STATUSES:
            raise ValueError(f"candidate status must be one of {CANDIDATE_STATUSES}, got {fields.get('status')!r}")
        cid = new_id()
        self._add("candidates", {"candidate_id": cid, "university_qid": self.university_qid,
                                 "created_at": now_iso(), "legacy": False, **fields})
        return cid

    def add_evidence(self, *, url: str, http_status: Optional[int], content: Optional[bytes],
                     unit_names=()) -> str:
        """Add one evidence row with a saved snapshot. Nothing calls this yet: pages are
        fetched from Anya's week 4 on."""
        eid = new_id()
        snapshot = None
        if content is not None:
            snapshot = self.save_text(f"evidence/{eid}.html", content.decode("utf-8", "replace"), "text/html")
        self._add("evidence", {"evidence_id": eid, "url": url, "fetched_at": now_iso(),
                               "http_status": http_status,
                               "content_hash": hashlib.sha256(content).hexdigest() if content is not None else None,
                               "snapshot_path": snapshot, "unit_names": list(unit_names)})
        return eid

    # ── writing ──────────────────────────────────────────────────────────

    def _write_local(self, rows: Dict[str, List[dict]]) -> None:
        self.local_dir.mkdir(parents=True, exist_ok=True)
        for t in TABLES:
            with (self.local_dir / f"{t}.jsonl").open("a") as f:
                for r in rows.get(t, []):
                    f.write(json.dumps(r, default=str) + "\n")
        self.local_written = True

    def _load_bigquery(self, rows: Dict[str, List[dict]]) -> None:
        from google.cloud import bigquery
        client = self._get_bq()
        for t in TABLES:
            if not rows.get(t):
                continue
            job = client.load_table_from_json(
                rows[t], table_ref(t),
                job_config=bigquery.LoadJobConfig(schema=bigquery_schema(t),
                                                  write_disposition="WRITE_APPEND"),
                timeout=_BQ_TIMEOUT_S)
            job.result(timeout=_BQ_TIMEOUT_S)

    def flush(self) -> None:
        """Send the pending rows to BigQuery. Rows also go to local JSON when BigQuery
        failed, or when any text of this invocation is only on local disk (a BigQuery
        reader could not open that path). Never raises."""
        rows = {t: self.pending[t] for t in TABLES}
        self.pending = {t: [] for t in TABLES}
        if not self.enabled or not any(rows.values()):
            return
        if not self.bq_error:
            try:
                self._load_bigquery(rows)
            except Exception as e:  # noqa: BLE001 - fall back to local files
                self.bq_error = f"{type(e).__name__}: {str(e)[:200]}"
                logger.warning("run log: BigQuery write failed, keeping rows locally from now on (%s)", self.bq_error)
        if self.bq_error or self.gcs_error:
            try:
                self._write_local(rows)
            except Exception as e:  # noqa: BLE001
                logger.error("run log: could not write local rows either (%s: %s)", type(e).__name__, e)

    def finish(self, outcome: str, summary: Optional[dict] = None) -> dict:
        """Write the runs row and everything still pending. Returns where it all went."""
        global _current
        self.run_row.update({"ended_at": now_iso(), "outcome": outcome, "summary": summary or {}})
        problems = {k: v for k, v in (("bucket", self.gcs_error), ("bigquery", self.bq_error)) if v}
        self.run_row["storage_problems"] = problems or None
        self._add("runs", self.run_row)
        self.flush()
        if self.enabled and self.local_written:
            # All four files exist, even an empty evidence.jsonl, so a reader can tell
            # "nothing fetched" from "file lost".
            self.local_dir.mkdir(parents=True, exist_ok=True)
            for t in TABLES:
                (self.local_dir / f"{t}.jsonl").touch()
        if _current is self:
            _current = None
        return self.status()

    def status(self) -> dict:
        return {"run_id": self.run_id, "invocation_id": self.invocation_id,
                "bigquery": "failed: " + self.bq_error if self.bq_error else "ok",
                "bucket": "failed: " + self.gcs_error if self.gcs_error else "ok",
                "local_dir": str(self.local_dir) if self.local_written else None,
                "rows": dict(self.counts)}


def start(run_id: str, invocation_id: Optional[str] = None, **kwargs) -> RunLog:
    """Create the run log for this invocation and make it current."""
    global _current
    log = RunLog(run_id, invocation_id, enabled=not DISABLED, **kwargs)
    _current = log if log.enabled else None
    return log


def describe(status: dict) -> str:
    """One line for the console about where the run log went."""
    rows = status["rows"]
    bq = "ok" if status["bigquery"] == "ok" else "FAILED"
    bucket = "ok" if status["bucket"] == "ok" else "FAILED"
    text = (f"Run log {status['run_id']}: {rows['llm_calls']} LLM calls, {rows['candidates']} candidates, "
            f"{rows['evidence']} evidence rows. BigQuery {config.GCP_PROJECT}.{config.RUNLOG_DATASET}: {bq}. "
            f"Bucket {config.RUNLOG_BUCKET}: {bucket}.")
    if status["local_dir"]:
        text += f" Local copy: {status['local_dir']}"
    return text


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        for t in setup_tables():
            print("ready:", t)
    else:
        print(__doc__)
        sys.exit(2)
