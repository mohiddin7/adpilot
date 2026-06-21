"""In-process DuckDB source. The pack's init SQL builds views over local CSVs — no credentials."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from adpilot.core.errors import AdPilotError


class DuckDBSource:
    dialect = "duckdb"

    def __init__(self, csv_dir: Path, init_sql: str) -> None:
        self._con = duckdb.connect()
        self._con.execute(init_sql.replace("{csv_dir}", Path(csv_dir).as_posix()))

    def query(self, sql: str, max_bytes: int | None = None) -> pd.DataFrame:
        try:
            return self._con.execute(sql).df()
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
