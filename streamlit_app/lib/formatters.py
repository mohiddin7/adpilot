"""
lib/formatters.py — Display formatting for dashboard values.

Why this module exists:
  Big raw numbers ($130,244.90) look amateur. Senior analyst dashboards
  show compact forms ($130.2K) with the full value in tooltips.

Conventions used:
  - Currency under $1K       → $987
  - Currency $1K to <$1M     → $130.2K
  - Currency $1M+            → $1.3M
  - Counts                   → 13.4K, 1.2M
  - Percentages              → 9.8%
  - Deltas                   → +12.3% / -4.1% (with sign)
"""
from __future__ import annotations

from typing import Optional


# ── Core compact formatter ───────────────────────────────────────────────────

def _compact(value: float, decimals: int = 1) -> tuple[float, str]:
    """Return (scaled_value, suffix) for a number. -1234567 → (-1.234567, 'M')."""
    abs_v = abs(value)
    if abs_v >= 1_000_000_000:
        return value / 1_000_000_000, "B"
    if abs_v >= 1_000_000:
        return value / 1_000_000, "M"
    if abs_v >= 1_000:
        return value / 1_000, "K"
    return value, ""


# ── Public formatters ─────────────────────────────────────────────────────────

def fmt_currency(value: Optional[float], decimals: int = 1) -> str:
    """
    $130244.90 → "$130.2K"   $1349.50 → "$1.3K"   $42.10 → "$42"
    None/NaN → "—".
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v != v:  # NaN check
        return "—"

    scaled, suffix = _compact(v, decimals)
    if suffix:
        return f"${scaled:.{decimals}f}{suffix}"
    # Below $1K — no decimals needed for whole-dollar amounts
    if abs(v) < 10:
        return f"${v:.2f}"
    return f"${v:.0f}"


def fmt_currency_full(value: Optional[float]) -> str:
    """Full form: $130,244.90 — for tooltips, source data tables, and exact reconciliation."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v != v:
        return "—"
    return f"${v:,.2f}"


def fmt_number(value: Optional[float], decimals: int = 1) -> str:
    """13363 → "13.4K"   542 → "542"   None → "—"."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v != v:
        return "—"

    scaled, suffix = _compact(v, decimals)
    if suffix:
        return f"{scaled:.{decimals}f}{suffix}"
    return f"{int(v):,}" if v == int(v) else f"{v:,.{decimals}f}"


def fmt_pct(value: Optional[float], decimals: int = 1, already_pct: bool = False) -> str:
    """
    Format a ratio or percentage.
      fmt_pct(0.0975)            → "9.8%"   (ratio → percent)
      fmt_pct(9.75, already_pct=True) → "9.8%"
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v != v:
        return "—"
    if not already_pct:
        v = v * 100
    return f"{v:.{decimals}f}%"


def fmt_delta(value: Optional[float], decimals: int = 1, as_pct: bool = True) -> str:
    """
    Signed change indicator.
      fmt_delta(0.123) → "+12.3%"   fmt_delta(-0.04) → "-4.0%"
    """
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v != v:
        return ""

    if as_pct:
        v = v * 100
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.{decimals}f}%"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:,.{decimals}f}"


def fmt_int(value: Optional[int]) -> str:
    """1234 → "1,234". None → "—"."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


# ── DataFrame column formatting helpers ──────────────────────────────────────

def currency_columns(df, columns: list[str]) -> dict:
    """Return st.column_config formatters for currency columns."""
    import streamlit as st
    return {c: st.column_config.NumberColumn(format="$%.2f") for c in columns if c in df.columns}


def percent_columns(df, columns: list[str], decimals: int = 1) -> dict:
    """Return st.column_config formatters for percent columns."""
    import streamlit as st
    fmt = f"%.{decimals}f%%"
    return {c: st.column_config.NumberColumn(format=fmt) for c in columns if c in df.columns}