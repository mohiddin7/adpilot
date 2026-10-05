"""BigQuery source with a per-query bytes-billed cap. Client is created lazily so importing is free."""

from __future__ import annotations

import concurrent.futures
import threading

import pandas as pd

from adpilot.connectors.base import timeout_error
from adpilot.core.errors import AdPilotError


class BigQuerySource:
    dialect = "bigquery"

    def __init__(self, project: str, default_max_bytes: int = 100 * 1024 * 1024, timeout_s: float = 30) -> None:
        self._project = project
        self._default_max_bytes = default_max_bytes
        self._timeout_s = timeout_s
        self._client = None
        self._client_lock = threading.Lock()  # panels run in parallel: create the client once

    @property
    def client(self):
        with self._client_lock:
            if self._client is None:
                from google.cloud import bigquery

                try:
                    self._client = bigquery.Client(project=self._project)
                except Exception as exc:  # auth / env problems
                    raise AdPilotError("DataSourceUnavailable", f"BigQuery client failed: {exc}") from exc
            return self._client

    def query(self, sql: str, max_bytes: int | None = None) -> pd.DataFrame:
        from google.cloud import bigquery

        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes or self._default_max_bytes,
                                      job_timeout_ms=int(self._timeout_s * 1000))
        # DEFAULT_RETRY's own deadline is 10 minutes: unbounded here would let a stalled HTTP call (not the
        # query itself) hold a panel worker far past our deadline. `retry` below bounds every RPC `client.query`/
        # `job.result` make (per google-cloud-bigquery 3.45.1's own `client.query`/`QueryJob.result` signatures).
        retry = bigquery.DEFAULT_RETRY.with_timeout(self._timeout_s)
        try:
            job = self.client.query(sql, job_config=cfg, timeout=self._timeout_s, retry=retry)
            try:
                rows = job.result(timeout=self._timeout_s + 5, retry=retry)  # +5: the server stops it at the deadline; this is the backstop
            except concurrent.futures.TimeoutError as exc:
                try:
                    job.cancel()
                except Exception:  # a failed cancel must never hide the real timeout
                    pass
                raise timeout_error(self._timeout_s) from exc
            return rows.to_dataframe()
        except AdPilotError:
            raise
        except Exception as exc:
            raise _map_bq_error(exc, self._timeout_s) from exc

    def list_tables(self) -> list[str]:
        # Tables are declared by the pack; listing every dataset in a project is not useful here.
        return []

    def columns(self, table: str) -> list[tuple[str, str]]:
        try:
            return [(f.name, f.field_type) for f in self.client.get_table(table).schema]
        except AdPilotError:
            raise
        except Exception as exc:
            raise _map_bq_error(exc, self._timeout_s) from exc


def _map_bq_error(exc: Exception, timeout_s: float = 30) -> AdPilotError:
    from google.api_core import exceptions as gexc

    msg = str(exc).strip().splitlines()[0] if str(exc) else exc.__class__.__name__
    if "timed out" in msg.lower():
        # Likely google.api_core.exceptions.GoogleAPICallError, unconfirmed against a live job: job_timeout_ms
        # reason "stopped" maps to HTTP 200, unregistered in job/base.py (google-cloud-bigquery 3.45.1).
        return timeout_error(timeout_s)
    if isinstance(exc, gexc.NotFound):
        return AdPilotError("SqlSchema", msg)
    if isinstance(exc, gexc.BadRequest):
        lowered = msg.lower()
        if "unrecognized name" in lowered or "not found" in lowered or "neither grouped" in lowered:
            return AdPilotError("SqlSchema", msg)
        return AdPilotError("SqlSyntax", msg)
    return AdPilotError("DataSourceUnavailable", msg)
