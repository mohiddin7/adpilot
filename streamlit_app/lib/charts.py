"""A chart spec (adpilot.core.chart.ChartSpec, as JSON from the API) plus its rows → a Plotly figure."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .theme import SERIES


def build_figure(chart: dict, rows: list[dict]) -> go.Figure | None:
    """None when there is nothing to draw; the caller shows the panel's note instead."""
    if not rows:
        return None
    df = pd.DataFrame(rows)
    kind, x, y = chart["chart_type"], chart["x"], chart["y"]
    if x not in df.columns or y not in df.columns:
        return None
    color = chart.get("color") if chart.get("color") in df.columns else None
    style = {"color_discrete_sequence": SERIES}
    if kind == "pie":
        fig = px.pie(df, names=x, values=y, **style)
    elif kind == "bar":
        fig = px.bar(df, x=x, y=y, color=color, barmode="group", **style)
    elif kind == "line":
        fig = px.line(df, x=x, y=y, color=color, markers=True, **style)
    elif kind == "area":
        fig = px.area(df, x=x, y=y, color=color, **style)
    elif kind == "scatter":
        fig = px.scatter(df, x=x, y=y, color=color, **style)
    else:
        return None
    fig.update_layout(height=300, margin={"l": 8, "r": 8, "t": 8, "b": 8}, legend_title_text="",
                      template="plotly_white")
    return fig
