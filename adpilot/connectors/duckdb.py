"""In-process DuckDB source. The pack's init SQL builds views over local CSVs — no credentials."""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb
import pandas as pd

from adpilot.connectors.base import timeout_error
from adpilot.core.errors import AdPilotError


class DuckDBSource:
    dialect = "duckdb"

    def __init__(self, csv_dir: Path, init_sql: str, timeout_s: float = 30) -> None:
        self._timeout_s = timeout_s
        self._con = duckdb.connect()
        self._con.execute(init_sql.replace("{csv_dir}", Path(csv_dir).as_posix()))
        # Load every view's rows now (a view over read_csv would read the file on each query), then close file
        # access for good: no later query, validated or not, can read a file or turn access back on.
        for (view,) in self._con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall():
            q = '"' + view.replace('"', '""') + '"'
            self._con.execute(f"CREATE TABLE _adpilot_load AS FROM {q}; DROP VIEW {q}; ALTER TABLE _adpilot_load RENAME TO {q}")
        self._con.execute("SET enable_external_access = false; SET lock_configuration = true")
        self._lock = threading.Lock()  # one shared connection; the eval harness can query it from two threads

    def execute_script(self, sql: str) -> None:
        """Run trusted setup SQL (fixtures). Never called with model-written text."""
        self._con.execute(sql)

    def query(self, sql: str, max_bytes: int | None = None) -> pd.DataFrame:
        try:
            with self._lock:
                watchdog = threading.Timer(self._timeout_s, self._con.interrupt)  # an idle interrupt is a no-op
                watchdog.start()
                try:
                    return self._con.execute(sql).df()
                finally:
                    watchdog.cancel()
        except duckdb.InterruptException as exc:  # before duckdb.Error: it is a subclass
            raise timeout_error(self._timeout_s) from exc
        except duckdb.ParserException as exc:
            raise AdPilotError("SqlSyntax", _first_line(exc)) from exc
        except duckdb.BinderException as exc:
            raise AdPilotError("SqlSchema", _first_line(exc), hint=_candidates(exc)) from exc
        except duckdb.CatalogException as exc:
            raise AdPilotError("SqlSchema", _first_line(exc)) from exc
        except duckdb.Error as exc:
            raise AdPilotError("DataSourceUnavailable", _first_line(exc)) from exc

    def list_tables(self) -> list[str]:
        rows = self._con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY 1"
        ).fetchall()
        return [r[0] for r in rows]

    def columns(self, table: str) -> list[tuple[str, str]]:
        rows = self._con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        return [(r[0], r[1]) for r in rows]


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0]


def _candidates(exc: Exception) -> str:
    lines = str(exc).splitlines()
    return next((line.strip() for line in lines if "Candidate" in line), "")
