"""BigQuery audit sink: strict preflight (auth, dataset, additive schema), free atomic load jobs with retry.

Schema declared here is the source of truth. JSON-valued columns are STRING holding JSON text.
"""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage

from adpilot.core.audit import AuditConfig, AuditRecord, AuditUnavailable, FlushReport, RunRow, ScoreRow

TRANSIENT_MAX_ATTEMPTS = 3
BACKOFF_S = (1, 4, 16)
STAGING_DATASET_ENV_DEFAULT = "adpilot_staging"
_CREDENTIALS_HINT = "set GOOGLE_APPLICATION_CREDENTIALS (service-account key path) or GCP_SERVICE_ACCOUNT_JSON, or run `gcloud auth application-default login`"
_PERMISSIONS_HINT = "grant the service account roles/bigquery.dataEditor and roles/bigquery.jobUser on the project"


@dataclass(frozen=True)
class TableSpec:
    name: str
    fields: list[tuple[str, str, str]]  # (name, type, mode)
    clustering: list[str]


_COMMON_TAIL = [("attributes", "STRING", "NULLABLE"), ("schema_version", "INT64", "NULLABLE")]

TABLES: dict[str, TableSpec] = {
    "agent_calls": TableSpec(
        "agent_calls",
        [
            ("trace_id", "STRING", "REQUIRED"), ("otel_trace_id", "STRING", "NULLABLE"), ("ts", "TIMESTAMP", "REQUIRED"),
            ("environment", "STRING", "NULLABLE"), ("source", "STRING", "NULLABLE"), ("session_id", "STRING", "NULLABLE"),
            ("run_id", "STRING", "NULLABLE"), ("case_name", "STRING", "NULLABLE"), ("family", "STRING", "NULLABLE"),
            ("pack", "STRING", "NULLABLE"), ("git_sha", "STRING", "NULLABLE"), ("prompt_hash", "STRING", "NULLABLE"),
            ("question", "STRING", "NULLABLE"), ("answer_md", "STRING", "NULLABLE"), ("sql", "STRING", "NULLABLE"),
            ("refused", "BOOL", "NULLABLE"), ("confidence", "FLOAT64", "NULLABLE"), ("caveats", "STRING", "REPEATED"),
            ("error_kind", "STRING", "NULLABLE"), ("model_requested", "STRING", "NULLABLE"), ("model_used", "STRING", "NULLABLE"),
            ("fell_back", "BOOL", "NULLABLE"), ("requests", "INT64", "NULLABLE"), ("tool_calls", "STRING", "REPEATED"),
            ("repairs", "INT64", "NULLABLE"), ("tokens_in", "INT64", "NULLABLE"), ("tokens_out", "INT64", "NULLABLE"),
            ("cost_usd", "FLOAT64", "NULLABLE"), ("latency_s", "FLOAT64", "NULLABLE"), ("messages_json", "STRING", "NULLABLE"),
            *_COMMON_TAIL,
        ],
        ["source", "run_id"],
    ),
    "scores": TableSpec(
        "scores",
        [
            ("trace_id", "STRING", "REQUIRED"), ("run_id", "STRING", "NULLABLE"), ("name", "STRING", "REQUIRED"),
            ("value", "FLOAT64", "NULLABLE"), ("passed", "BOOL", "NULLABLE"), ("source", "STRING", "NULLABLE"),
            ("grader", "STRING", "NULLABLE"), ("reason", "STRING", "NULLABLE"), ("ts", "TIMESTAMP", "REQUIRED"),
            *_COMMON_TAIL,
        ],
        ["run_id", "name"],
    ),
    "eval_runs": TableSpec(
        "eval_runs",
        [
            ("run_id", "STRING", "REQUIRED"), ("ts", "TIMESTAMP", "REQUIRED"), ("environment", "STRING", "NULLABLE"),
            ("tier", "STRING", "NULLABLE"), ("git_sha", "STRING", "NULLABLE"), ("prompt_hash", "STRING", "NULLABLE"),
            ("pack", "STRING", "NULLABLE"), ("models", "STRING", "NULLABLE"), ("case_count", "INT64", "NULLABLE"),
            ("overall", "FLOAT64", "NULLABLE"), ("gate_ok", "BOOL", "NULLABLE"), ("gate_reasons", "STRING", "REPEATED"),
            ("scorecard_json", "STRING", "NULLABLE"), ("invariants_json", "STRING", "NULLABLE"), ("calls_used", "INT64", "NULLABLE"),
            ("tokens_in", "INT64", "NULLABLE"), ("tokens_out", "INT64", "NULLABLE"), ("cost_usd", "FLOAT64", "NULLABLE"),
            ("duration_s", "FLOAT64", "NULLABLE"), *_COMMON_TAIL,
        ],
        ["tier"],
    ),
}


def _is_transient(exc: Exception) -> bool:
    from google.api_core import exceptions as gex

    if isinstance(exc, (gex.ServerError, gex.ServiceUnavailable, gex.TooManyRequests, ConnectionError, TimeoutError)):
        return True
    return isinstance(exc, gex.GoogleAPICallError) and "rateLimitExceeded" in str(exc)


class BigQuerySink:
    def __init__(self, cfg: AuditConfig, client=None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self._client = client
        self._sleep = sleep
        self._lock = threading.Lock()
        self._buffers: dict[str, list[dict]] = {"agent_calls": [], "scores": [], "eval_runs": []}
        self._pending_records: list[AuditRecord] = []

    # ----- client -----

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery
            from google.oauth2 import service_account

            try:
                if self.cfg.service_account_json:
                    info = json.loads(self.cfg.service_account_json)
                    creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
                    self._client = bigquery.Client(project=self.cfg.project, credentials=creds)
                else:  # GOOGLE_APPLICATION_CREDENTIALS or ADC — the google-auth default chain
                    self._client = bigquery.Client(project=self.cfg.project)
            except Exception as exc:  # noqa: BLE001 — mapped to a hint below
                raise AuditUnavailable("credentials", f"{exc.__class__.__name__}: {exc}; {_CREDENTIALS_HINT}") from exc
        return self._client

    @property
    def dataset_ref(self) -> str:
        return f"{self.cfg.project}.{self.cfg.dataset}"

    def _table_ref(self, name: str) -> str:
        return f"{self.dataset_ref}.{name}"

    # ----- preflight -----

    def preflight(self) -> None:
        from google.api_core import exceptions as gex
        from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
        from google.cloud import bigquery

        try:
            self._ensure_dataset()
            for spec in TABLES.values():
                self._ensure_table(spec)
            self.client.query("SELECT 1", job_config=bigquery.QueryJobConfig(dry_run=True))
        except AuditUnavailable:
            raise
        except (DefaultCredentialsError, GoogleAuthError) as exc:
            raise AuditUnavailable("credentials", f"{exc}; {_CREDENTIALS_HINT}") from exc
        except gex.Forbidden as exc:
            raise AuditUnavailable("permissions", f"{exc.message if hasattr(exc, 'message') else exc}; {_PERMISSIONS_HINT}") from exc
        except gex.NotFound as exc:
            raise AuditUnavailable("location", f"{exc}; check BQ_PROJECT_ID / BQ_LOCATION") from exc
        except gex.GoogleAPICallError as exc:
            raise AuditUnavailable("bigquery", str(exc)) from exc

    def _ensure_dataset(self) -> None:
        from google.api_core import exceptions as gex
        from google.cloud import bigquery

        try:
            self.client.get_dataset(self.dataset_ref)
            return
        except gex.NotFound:
            pass
        location = self.cfg.location or self._staging_location() or "US"
        ds = bigquery.Dataset(self.dataset_ref)
        ds.location = location
        self.client.create_dataset(ds, exists_ok=True)

    def _staging_location(self) -> str | None:
        """The pipeline's staging dataset already lives somewhere; the audit dataset goes next to it."""
        import os

        from google.api_core import exceptions as gex

        staging = os.environ.get("BQ_STAGING_DATASET") or STAGING_DATASET_ENV_DEFAULT
        try:
            return self.client.get_dataset(f"{self.cfg.project}.{staging}").location
        except gex.NotFound:
            return None

    def _ensure_table(self, spec: TableSpec) -> None:
        from google.api_core import exceptions as gex
        from google.cloud import bigquery

        ref = self._table_ref(spec.name)
        want = [bigquery.SchemaField(n, t, mode=m) for n, t, m in spec.fields]
        try:
            table = self.client.get_table(ref)
        except gex.NotFound:
            table = bigquery.Table(ref, schema=want)
            table.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.DAY, field="ts")
            table.clustering_fields = spec.clustering
            self.client.create_table(table)
            return
        have = {f.name: f for f in table.schema}
        for f in want:
            if f.name in have and have[f.name].field_type.upper() not in (f.field_type.upper(), _alias(f.field_type)):
                raise AuditUnavailable("schema", f"{spec.name}.{f.name} is {have[f.name].field_type} in BigQuery but the code expects {f.field_type}; migrations are additive only — add a new column instead")
        missing = [bigquery.SchemaField(f.name, f.field_type, mode="NULLABLE" if f.mode == "REQUIRED" else f.mode) for f in want if f.name not in have]
        if missing:
            table.schema = list(table.schema) + missing
            self.client.update_table(table, ["schema"])

    # ----- buffer -----

    def record(self, rec: AuditRecord) -> None:
        with self._lock:
            self._buffers["agent_calls"].append(rec.row())
            self._pending_records.append(rec)

    def add_scores(self, rows: Sequence[ScoreRow]) -> None:
        with self._lock:
            self._buffers["scores"].extend(r.row() for r in rows)

    def add_run(self, row: RunRow) -> None:
        with self._lock:
            self._buffers["eval_runs"].append(row.row())

    def pending_calls(self) -> list[AuditRecord]:
        return list(self._pending_records)

    # ----- flush -----

    def flush(self) -> FlushReport:
        report = FlushReport()
        for name in ("agent_calls", "scores", "eval_runs"):
            with self._lock:
                rows = list(self._buffers[name])
            if not rows:
                continue
            err = self._load_with_retry(name, rows)
            if err is None:
                with self._lock:
                    del self._buffers[name][: len(rows)]
                    if name == "agent_calls":
                        del self._pending_records[: len(rows)]
                report.written[name] = len(rows)
            else:
                report.failed[name] = len(rows)
                report.errors.append(f"{name}: {err}")
        return report

    def _load_with_retry(self, name: str, rows: list[dict]) -> str | None:
        last = "unknown"
        for attempt in range(TRANSIENT_MAX_ATTEMPTS):
            try:
                self._load(name, rows)
                return None
            except Exception as exc:  # noqa: BLE001 — classified below
                last = f"{exc.__class__.__name__}: {exc}"[:300]
                if not _is_transient(exc) or attempt == TRANSIENT_MAX_ATTEMPTS - 1:
                    return last
                self._sleep(BACKOFF_S[attempt])
        return last

    def _load(self, name: str, rows: list[dict]) -> None:
        from google.cloud import bigquery

        spec = TABLES[name]
        data = "\n".join(json.dumps(r, default=str) for r in rows).encode()
        cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=[bigquery.SchemaField(n, t, mode=m) for n, t, m in spec.fields],
            schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
        )
        job = self.client.load_table_from_file(io.BytesIO(data), self._table_ref(name), job_config=cfg)
        job.result(timeout=120)

    # ----- reads -----

    def load_session(self, session_id: str, turns: int = 3) -> list[ModelMessage]:
        from google.cloud import bigquery

        sql = f"SELECT messages_json FROM `{self._table_ref('agent_calls')}` WHERE session_id = @sid AND source = 'chat' ORDER BY ts DESC LIMIT @n"
        cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("sid", "STRING", session_id), bigquery.ScalarQueryParameter("n", "INT64", turns)])
        rows = list(self.client.query(sql, job_config=cfg).result())
        history: list[ModelMessage] = []
        for r in reversed(rows):
            if r["messages_json"]:
                history.extend(ModelMessagesTypeAdapter.validate_json(r["messages_json"]))
        return history

    def list_runs(self, limit: int = 20) -> list[dict]:
        from google.cloud import bigquery

        sql = f"SELECT run_id, ts, environment, tier, overall, gate_ok, gate_reasons, calls_used, cost_usd, duration_s FROM `{self._table_ref('eval_runs')}` ORDER BY ts DESC LIMIT @n"
        cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("n", "INT64", limit)])
        return [dict(r) for r in self.client.query(sql, job_config=cfg).result()]

    def export_run(self, run_id: str) -> Iterator[dict]:
        from google.cloud import bigquery

        sql = (
            f"SELECT c.*, ARRAY(SELECT AS STRUCT s.name, s.value, s.passed, s.source, s.grader, s.reason FROM `{self._table_ref('scores')}` s WHERE s.trace_id = c.trace_id) AS scores "
            f"FROM `{self._table_ref('agent_calls')}` c WHERE c.run_id = @run ORDER BY c.ts"
        )
        cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("run", "STRING", run_id)])
        for r in self.client.query(sql, job_config=cfg).result():
            yield dict(r)


def _alias(field_type: str) -> str:
    """BigQuery reports legacy names for some types (FLOAT for FLOAT64, INTEGER for INT64, BOOLEAN for BOOL)."""
    return {"FLOAT64": "FLOAT", "INT64": "INTEGER", "BOOL": "BOOLEAN"}.get(field_type.upper(), field_type.upper())
