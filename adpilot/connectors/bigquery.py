"""BigQuery source with a per-query bytes-billed cap. Client is created lazily so importing is free."""

from __future__ import annotations

import pandas as pd

from adpilot.core.errors import AdPilotError


class BigQuerySource:
    dialect = "bigquery"

    def __init__(self, project: str, default_max_bytes: int = 100 * 1024 * 1024) -> None:
        self._project = project
        self._default_max_bytes = default_max_bytes
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery

            try:
                self._client = bigquery.Client(project=self._project)
            except Exception as exc:  # auth / env problems
                raise AdPilotError("DataSourceUnavailable", f"BigQuery client failed: {exc}") from exc
        return self._client

    def query(self, sql: str, max_bytes: int | None = None) -> pd.DataFrame:
        from google.cloud import bigquery

        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes or self._default_max_bytes)
        try:
            return self.client.query(sql, job_config=cfg).to_dataframe()
        except AdPilotError:
            raise
        except Exception as exc:
            raise _map_bq_error(exc) from exc

    def list_tables(self) -> list[str]:
        # Tables are declared by the pack; listing every dataset in a project is not useful here.
        return []

    def columns(self, table: str) -> list[tuple[str, str]]:
        try:
            return [(f.name, f.field_type) for f in self.client.get_table(table).schema]
        except AdPilotError:
            raise
        except Exception as exc:
            raise _map_bq_error(exc) from exc


def _map_bq_error(exc: Exception) -> AdPilotError:
    from google.api_core import exceptions as gexc

    msg = str(exc).strip().splitlines()[0] if str(exc) else exc.__class__.__name__
    if isinstance(exc, gexc.NotFound):
        return AdPilotError("SqlSchema", msg)
    if isinstance(exc, gexc.BadRequest):
        lowered = msg.lower()
        if "unrecognized name" in lowered or "not found" in lowered or "neither grouped" in lowered:
            return AdPilotError("SqlSchema", msg)
        return AdPilotError("SqlSyntax", msg)
    return AdPilotError("DataSourceUnavailable", msg)
