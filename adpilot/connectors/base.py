from __future__ import annotations

from typing import Protocol

import pandas as pd


class DataSource(Protocol):
    """Read-only SQL source. Implementations raise AdPilotError, never driver exceptions."""

    dialect: str

    def query(self, sql: str, max_bytes: int | None = None) -> pd.DataFrame: ...

    def list_tables(self) -> list[str]: ...

    def columns(self, table: str) -> list[tuple[str, str]]: ...
