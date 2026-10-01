from __future__ import annotations

from typing import Protocol

import pandas as pd

from adpilot.core.errors import AdPilotError


class DataSource(Protocol):
    """Read-only SQL source. Implementations raise AdPilotError, never driver exceptions."""

    dialect: str

    def query(self, sql: str, max_bytes: int | None = None) -> pd.DataFrame: ...

    def list_tables(self) -> list[str]: ...

    def columns(self, table: str) -> list[tuple[str, str]]: ...


TIMEOUT_HINT = "Make the query cheaper: aggregate, narrow the date range, and avoid self-joins or cross joins."


def timeout_error(seconds: float) -> AdPilotError:
    return AdPilotError("QueryTimeout", f"The query ran past {seconds:g} seconds and was stopped.", hint=TIMEOUT_HINT)
