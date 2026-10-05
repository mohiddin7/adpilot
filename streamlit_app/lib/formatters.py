"""Formats values for tiles, tables and charts.

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

def fmt_currency(value: float | None, decimals: int = 1) -> str:
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


def fmt_number(value: float | None, decimals: int = 1) -> str:
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


def fmt_pct(value: float | None, decimals: int = 1, already_pct: bool = False) -> str:
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


LABELS = {"spend": "Spend", "conversions": "Conversions", "cpa": "Cost per acquisition", "ctr": "Click-through rate",
          "cvr": "Conversion rate", "roas_google": "ROAS (Google)", "roas": "ROAS", "cpc": "Cost per click",
          "cpm": "Cost per 1,000 impressions", "impressions": "Impressions", "clicks": "Clicks",
          "quality_score": "Quality score", "impression_share": "Search impression share",
          "search_impression_share": "Search impression share", "frequency": "Frequency",
          "excess_cost": "Excess cost", "flagged_days": "Flagged days", "vs_account": "Against the account"}


def label(column: str) -> str:
    return LABELS.get(column, column.replace("_", " ").capitalize())


def fmt(value, kind: str | None) -> str:
    """One value in the style its panel's `formats` names; an unknown or missing kind is a plain number."""
    if kind == "currency":
        return fmt_currency(value)
    if kind == "percent":
        return fmt_pct(value, decimals=2)
    if kind == "multiple":
        return "—" if value is None else f"{float(value):.2f}x"
    return fmt_number(value)


CURRENCY_COLUMNS = {"spend", "revenue", "cpa", "cpc", "cpm", "conversion_value"}


def format_for(column: str) -> str:
    """A column's format from its name alone, for the analyst's tables, which have no panel `formats`."""
    c = column.lower()
    if c in CURRENCY_COLUMNS or c.endswith("_cost"):
        return "currency"
    if c in ("ctr", "cvr") or c.endswith(("_rate", "_share")):
        return "percent"
    if c == "roas" or c.startswith("roas_"):
        return "multiple"
    return "number"
