"""Viewer filter values → one WHERE expression for pack-authored panel SQL.

DataSource.query() takes plain SQL text for both connectors (no bound parameters on that path), so a value is
validated first and quoted per dialect last. Quoting is not the only guard: dates must parse, numbers must be finite,
a filter with a fixed `values` list rejects anything else, the expression's length is capped, and the finished
statement still goes through validate_sql inside execute().

ponytail: validate_sql scans the whole statement, string literals included, for write keywords and for LIMIT, so a
real value like "Drop Shipping Sale" fails its panels with SqlPolicy (the page still loads; tests/test_api.py pins
it). Masking literals before those scans is a change to the model-SQL validator and gets its own review.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from adpilot.dashboard.config import DashboardConfig

MAX_VALUES_PER_FILTER = 25
MAX_VALUE_CHARS = 120
MAX_WHERE_CHARS = 1500  # validate_sql caps a whole statement at 4000 characters
MAX_RANGE_DAYS = 366  # inclusive; streamlit_app/lib/controls.py clamps to the same number


class FilterError(ValueError):
    """A viewer-supplied value the API refuses: an HTTP 422, never a query."""


def sql_literal(value: str, dialect: str) -> str:
    """Quote one string for the given dialect. Callers have already rejected control characters."""
    if dialect == "bigquery":
        # GoogleSQL takes backslash escapes and has no '' escape: 'it''s' is two adjacent literals, a syntax error.
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if dialect == "duckdb":
        # Standard SQL: '' is one quote and a backslash is an ordinary character.
        return "'" + value.replace("'", "''") + "'"
    raise ValueError(f"no literal quoting for dialect {dialect!r}")


@dataclass(frozen=True)
class Filters:
    """Validated filter state. Hashable (tuples all the way down) because it is part of the panel cache key."""

    date_from: date
    date_to: date
    categorical: tuple[tuple[str, tuple[str, ...]], ...] = ()
    ranges: tuple[tuple[str, float | None, float | None], ...] = ()

    def values(self, column: str) -> tuple[str, ...]:
        return dict(self.categorical).get(column, ())


def parse_filters(cfg: DashboardConfig, page: str, items: list[tuple[str, str]]) -> Filters:
    """Query-string pairs → Filters. Keys: date_from, date_to (required, YYYY-MM-DD), `<column>` (repeatable) for a
    categorical filter, `<column>_min` / `<column>_max` for a range. An unknown key is an error, not ignored: a
    misspelt filter that silently does nothing shows the viewer numbers they did not ask for."""
    declared = {f.column: f for f in cfg.filters_for(page)}
    got: dict[str, list[str]] = {}
    for key, value in items:
        got.setdefault(key, []).append(value)
    known = {"date_from", "date_to"} | {c for c, f in declared.items() if f.type == "categorical"}
    known |= {f"{c}_{end}" for c, f in declared.items() if f.type == "range" for end in ("min", "max")}
    unknown = sorted(set(got) - known)
    if unknown:
        raise FilterError(f"unknown filter(s) for the {page} page: {', '.join(unknown)}")

    date_from, date_to = _date(got, "date_from"), _date(got, "date_to")
    if date_from > date_to:
        raise FilterError("date_from is after date_to")
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise FilterError(f"the date range is longer than {MAX_RANGE_DAYS} days")

    categorical, ranges = [], []
    for column, f in declared.items():
        if f.type == "categorical" and column in got:
            chosen = tuple(sorted(set(got[column])))
            if len(chosen) > MAX_VALUES_PER_FILTER:
                raise FilterError(f"at most {MAX_VALUES_PER_FILTER} values for {column}")
            for v in chosen:
                if not v or len(v) > MAX_VALUE_CHARS or any(ord(c) < 32 or ord(c) == 127 for c in v):
                    raise FilterError(f"{column}: each value must be 1-{MAX_VALUE_CHARS} printable characters")
                if f.values is not None and v not in f.values:
                    raise FilterError(f"{column} must be one of: {', '.join(f.values)}")
            categorical.append((column, chosen))
        elif f.type == "range":
            lo, hi = _number(got, f"{column}_min"), _number(got, f"{column}_max")
            if lo is not None and hi is not None and lo > hi:
                raise FilterError(f"{column}_min is above {column}_max")
            if lo is not None or hi is not None:
                ranges.append((column, lo, hi))
    return Filters(date_from, date_to, tuple(categorical), tuple(ranges))


def build_where(cfg: DashboardConfig, table: str, date_column: str, flt: Filters, dialect: str,
                only: set[str] | None = None) -> str:
    """The expression that replaces `{where}`: the date range, then every set filter that applies to `table`.
    `only` narrows the non-date conditions to those columns (the cascade in panels.filter_options)."""
    parts = [f"{date_column} BETWEEN DATE '{flt.date_from.isoformat()}' AND DATE '{flt.date_to.isoformat()}'"]
    for column, chosen in flt.categorical:
        if table in cfg.filter(column).tables and (only is None or column in only):
            parts.append(f"{column} IN ({', '.join(sql_literal(v, dialect) for v in chosen)})")
    for column, lo, hi in flt.ranges:
        if table in cfg.filter(column).tables and (only is None or column in only):
            if lo is not None:
                parts.append(f"{column} >= {lo!r}")
            if hi is not None:
                parts.append(f"{column} <= {hi!r}")
    where = " AND ".join(parts)
    if len(where) > MAX_WHERE_CHARS:
        raise FilterError("too many filter values selected; narrow the selection")
    return where


def _one(got: dict[str, list[str]], key: str) -> str | None:
    values = got.get(key)
    if not values:
        return None
    if len(values) > 1:
        raise FilterError(f"{key} is given more than once")
    return values[0]


def _date(got: dict[str, list[str]], key: str) -> date:
    raw = _one(got, key)
    if raw is None:
        raise FilterError(f"{key} is required (YYYY-MM-DD)")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise FilterError(f"{key} must be a date (YYYY-MM-DD), got {raw[:20]!r}") from None


def _number(got: dict[str, list[str]], key: str) -> float | None:
    raw = _one(got, key)
    if raw is None:
        return None
    try:
        x = float(raw)
    except ValueError:
        raise FilterError(f"{key} must be a number") from None
    if not math.isfinite(x):
        raise FilterError(f"{key} must be a finite number")
    return x
