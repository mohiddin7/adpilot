"""
lib/chart_builder.py — Safe, constrained chart generation for the chatbot.

SECURITY MODEL:
  The LLM NEVER writes or executes Python. Instead, the LLM picks a "chart
  spec" — a JSON object with a small enum of chart types and column names
  that MUST exist in the actual DataFrame. This module validates every field
  of that spec against the real DataFrame BEFORE building anything, then
  calls one of a handful of pre-written Plotly builder functions.

  This is the same pattern already used in pages/2_💡_AI_Insights.py for the
  five insight cards — just generalized to work on arbitrary chat results.

  What this prevents:
    - Arbitrary code execution (no exec/eval anywhere)
    - Column-injection (every column name is checked against df.columns)
    - Chart-type injection (chart_type is checked against CHART_TYPES enum)

  What the LLM is allowed to choose:
    - chart_type:  one of CHART_TYPES
    - x, y:        column names that exist in the DataFrame
    - color:       optional column name that exists in the DataFrame
    - title:       a short string (HTML-escaped before display)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .sql_validator import html_escape_for_display

log = logging.getLogger(__name__)

# The ONLY chart types the LLM may select. Each maps to a builder function below.
CHART_TYPES = {"bar", "line", "scatter", "pie", "area"}

# Title length cap — prevents absurdly long LLM-generated titles
MAX_TITLE_LEN = 80


class ChartSpecError(Exception):
    """Raised when a chart spec references a column that doesn't exist,
    an unsupported chart type, or is otherwise malformed. Message is
    safe to show to the user."""


@dataclass(frozen=True)
class ChartSpec:
    chart_type: str
    x:          str
    y:          str
    color:      Optional[str] = None
    title:      str = ""


# ── Parsing + validation ──────────────────────────────────────────────────────

def parse_chart_spec(raw: str, df: pd.DataFrame) -> ChartSpec:
    """
    Parse and validate a chart spec JSON string against a real DataFrame.

    Raises ChartSpecError with a user-safe message on any problem:
      - invalid JSON
      - unknown chart_type
      - x / y / color reference a column not in df.columns
      - df has no rows
    """
    if df is None or df.empty:
        raise ChartSpecError("There's no data from the previous answer to chart.")

    raw = raw.strip()
    # Strip markdown fences defensively (some models wrap JSON in ```json)
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChartSpecError(f"Couldn't understand the chart request ({exc}).") from exc

    if not isinstance(obj, dict):
        raise ChartSpecError("Chart spec must be a JSON object.")

    chart_type = str(obj.get("chart_type", "")).strip().lower()
    if chart_type not in CHART_TYPES:
        raise ChartSpecError(
            f"'{chart_type}' isn't a supported chart type. "
            f"Supported: {', '.join(sorted(CHART_TYPES))}."
        )

    columns = set(df.columns)

    x = str(obj.get("x", "")).strip()
    if x not in columns:
        raise ChartSpecError(
            f"Column '{x}' isn't in the result data. "
            f"Available columns: {', '.join(df.columns)}."
        )

    y = str(obj.get("y", "")).strip()
    if y not in columns:
        raise ChartSpecError(
            f"Column '{y}' isn't in the result data. "
            f"Available columns: {', '.join(df.columns)}."
        )

    color = obj.get("color")
    if color is not None:
        color = str(color).strip()
        if color not in columns:
            # Don't hard-fail on a bad color — just drop it
            log.info("Chart spec color column %r not in df; ignoring", color)
            color = None

    title = str(obj.get("title", "")).strip()[:MAX_TITLE_LEN]

    return ChartSpec(chart_type=chart_type, x=x, y=y, color=color, title=title)


# ── Builders (one per chart type — fixed code, no LLM-generated logic) ───────

def build_figure(spec: ChartSpec, df: pd.DataFrame, platform_colors: dict) -> go.Figure:
    """
    Build a Plotly figure from a validated ChartSpec. Dispatches on
    spec.chart_type, which is guaranteed to be in CHART_TYPES.
    """
    title = html_escape_for_display(spec.title) if spec.title else None
    color_map = platform_colors if spec.color == "platform" else None

    common_kwargs = dict(
        data_frame=df,
        x=spec.x,
        y=spec.y,
        title=title,
        template="plotly_dark",
    )
    if spec.color:
        common_kwargs["color"] = spec.color
        if color_map:
            common_kwargs["color_discrete_map"] = color_map

    if spec.chart_type == "bar":
        fig = px.bar(**common_kwargs)
    elif spec.chart_type == "line":
        fig = px.line(**common_kwargs, markers=True)
    elif spec.chart_type == "scatter":
        fig = px.scatter(**common_kwargs)
    elif spec.chart_type == "area":
        fig = px.area(**common_kwargs)
    elif spec.chart_type == "pie":
        # Pie ignores `y` as a continuous axis — values come from spec.y,
        # labels from spec.x. color is not applicable.
        fig = px.pie(df, names=spec.x, values=spec.y, title=title,
                     template="plotly_dark")
    else:  # pragma: no cover — guarded by parse_chart_spec
        raise ChartSpecError(f"Unsupported chart type: {spec.chart_type}")

    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=40 if title else 8, b=0),
        height=360,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


# ── Heuristic fallback (no LLM available) ─────────────────────────────────────

def heuristic_chart_spec(df: pd.DataFrame) -> Optional[ChartSpec]:
    """
    When the LLM is unavailable, pick a reasonable chart spec using simple
    heuristics:
      - If there's a 'date' column → line chart of date vs. first numeric column
      - elif there's a 'platform' column → bar chart of platform vs. first numeric
      - elif first column is categorical + second is numeric → bar chart
      - else → None (can't guess)
    """
    if df is None or df.empty:
        return None

    cols = list(df.columns)
    numeric_cols = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    if not numeric_cols:
        return None

    if "date" in cols and numeric_cols:
        y = numeric_cols[0]
        return ChartSpec(chart_type="line", x="date", y=y,
                        color="platform" if "platform" in cols else None,
                        title=f"{y} over time")

    if "platform" in cols and numeric_cols:
        y = numeric_cols[0]
        return ChartSpec(chart_type="bar", x="platform", y=y, title=f"{y} by platform")

    # First categorical column + first numeric column
    categorical_cols = [c for c in cols if c not in numeric_cols]
    if categorical_cols and numeric_cols:
        return ChartSpec(chart_type="bar", x=categorical_cols[0], y=numeric_cols[0],
                        title=f"{numeric_cols[0]} by {categorical_cols[0]}")

    return None