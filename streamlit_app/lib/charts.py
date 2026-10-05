"""A chart spec (ChartSpec / PanelChartSpec as JSON from the API) plus its rows → a Plotly figure in one house style:
the palette's grounds, light gridlines, one font, fixed height, platform colours from the pack, and axes and hovers
formatted from the panel's `formats`."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .formatters import fmt, fmt_currency, label
from .theme import COLORS, GRID, GROUNDS, MUTED, OTHER, SERIES, SEVERITY, SYMBOLS, platform_colour

HEIGHT = 320
AXIS = {"currency": ("$,.0f", "$,.2f", ""), "percent": (".1%", ".2%", ""), "multiple": (".1f", ".2f", "x"),
        "number": (",.0f", ",.0f", "")}
WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
UNTITLED_X = {"date", "name", "period", "series", "measure", "week", "plan"}  # category holders: the ticks say it all
# x = spend, y = cost per acquisition: bottom is cheap, right is big.
QUADRANTS = (("scale", 0.01, 0.02, "left", "bottom"), ("watch", 0.99, 0.02, "right", "bottom"),
             ("fix", 0.01, 0.98, "left", "top"), ("cut", 0.99, 0.98, "right", "top"))


def style(fig: go.Figure, formats: dict | None = None, x: str | None = None, y: str | None = None) -> go.Figure:
    fig.update_layout(template="plotly_white", height=HEIGHT, margin={"l": 8, "r": 8, "t": 8, "b": 8},
                      paper_bgcolor=GROUNDS["surface"], plot_bgcolor=GROUNDS["surface"],
                      font={"family": "Inter, system-ui, sans-serif", "size": 12}, legend_title_text="",
                      legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0})  # above: clear of the x title
    fig.update_xaxes(gridcolor=GRID, title_text=label(x) if x and x not in UNTITLED_X else "")
    fig.update_yaxes(gridcolor=GRID, title_text=label(y) if y else "")
    for update, column in ((fig.update_xaxes, x), (fig.update_yaxes, y)):
        kind = (formats or {}).get(column)
        if kind in AXIS:
            tick, hover, suffix = AXIS[kind]
            update(tickformat=tick, hoverformat=hover, ticksuffix=suffix)
    return fig


def _colour(df: pd.DataFrame, column: str | None, colors: dict | None, single: str | None) -> dict:
    """Platform (or severity) colours when every value has one. Any other set of series gets OTHER, which no platform
    wears, in the order of the sorted names: the same name is the same colour on every page, whatever order the rows
    come in. One series alone is `single` (the one platform in view) or the brand."""
    if not column:
        return {"color_discrete_sequence": [single] if single else SERIES}
    if colors and set(df[column].dropna().astype(str)) <= set(colors):
        return {"color_discrete_map": colors}
    names = sorted(df[column].dropna().unique(), key=str)
    return {"color_discrete_map": {name: OTHER[k % len(OTHER)] for k, name in enumerate(names)}}


def build_figure(chart: dict, rows: list[dict], formats: dict | None = None, colors: dict | None = None,
                 single: str | None = None) -> go.Figure | None:
    """None when there is nothing to draw; the caller shows the panel's note instead. `single` is the colour of the
    one platform in view, for charts with a single series."""
    if not rows:
        return None
    df = pd.DataFrame(rows)
    kind, x, y = chart["chart_type"], chart["x"], chart["y"]
    size, z = chart.get("size"), chart.get("z")
    if any(c and c not in df.columns for c in (x, y, size, z)):
        return None
    color = chart.get("color") if chart.get("color") in df.columns else None
    if color is None and kind in ("bar", "bar_h") and colors and set(df[x].dropna().astype(str)) <= set(colors):
        color = x  # one series whose categories are all platforms: each bar wears its platform's colour
    hover = next((c for c in ("sub_group_name", "campaign_name") if c in df.columns), None)
    if kind == "pie":
        fig = px.pie(df, names=x, values=y, color=x, **_colour(df, x, colors, single))
    elif kind == "bar":
        fig = px.bar(df, x=x, y=y, color=color, barmode="relative" if color == x else "group",
                     **_colour(df, color, colors, single))
    elif kind == "bar_h":
        fig = px.bar(df, x=y, y=x, color=color, orientation="h", **_colour(df, color, colors, single))
        fig.update_yaxes(autorange="reversed")
        return style(fig, formats, x=y)
    elif kind == "line":
        fig = px.line(df, x=x, y=y, color=color, markers=True, **_colour(df, color, colors, single))
    elif kind == "area":
        fig = px.area(df, x=x, y=y, color=color, **_colour(df, color, colors, single))
    elif kind in ("scatter", "bubble"):
        fig = px.scatter(df, x=x, y=y, color=color, size=size if kind == "bubble" else None, size_max=40,
                         hover_name=hover, **_colour(df, color, colors, single))
    elif kind == "funnel":
        fig = go.Figure(go.Funnel(y=df[x], x=df[y], text=[fmt(v, (formats or {}).get(y)) for v in df[y]],
                                  textinfo="text+percent previous", marker={"color": single or COLORS["brand"]}))
        return style(fig)
    elif kind == "heatmap":
        grid = df.pivot_table(index=y, columns=x, values=z, aggfunc="sum")
        if set(grid.columns) <= set(WEEK):
            grid = grid[[d for d in WEEK if d in grid.columns]]
        tick, hover, suffix = AXIS.get((formats or {}).get(z), ("", "", ""))
        fig = go.Figure(go.Heatmap(z=grid.values, x=list(grid.columns), y=list(grid.index),
                                   colorscale=[[0, GROUNDS["bg"]], [1, single or COLORS["brand"]]],
                                   colorbar={"tickformat": tick, "ticksuffix": suffix},
                                   hovertemplate=f"%{{y}} · %{{x}}: %{{z{':' + hover if hover else ''}}}{suffix}"
                                                 "<extra></extra>"))
        return style(fig)
    else:
        return None
    if chart.get("reference") == "mean_y":
        _reference(fig, df, y, size, formats, quadrants=kind == "bubble", x=x)
    return style(fig, formats, x=x, y=y)


def _reference(fig: go.Figure, df: pd.DataFrame, y: str, size: str | None, formats: dict | None, quadrants: bool,
               x: str) -> None:
    ys = pd.to_numeric(df[y], errors="coerce")
    if size:
        w = pd.to_numeric(df[size], errors="coerce").where(ys.notna())
        ref = float((ys * w).sum() / w.sum()) if w.sum() else float(ys.mean())
    else:
        ref = float(ys.mean())
    fig.add_hline(y=ref, line_dash="dash", line_color=MUTED,
                  annotation_text=f"average {fmt(ref, (formats or {}).get(y))}", annotation_position="top left")
    if quadrants:
        fig.add_vline(x=float(pd.to_numeric(df[x], errors="coerce").median()), line_dash="dot", line_color=GRID)
        for text, px_, py_, xa, ya in QUADRANTS:
            fig.add_annotation(text=text, xref="paper", yref="paper", x=px_, y=py_, xanchor=xa, yanchor=ya,
                               showarrow=False, font={"color": MUTED, "size": 13})


def add_prior(fig: go.Figure, rows: list[dict], x: str, y: str, shift_days: int) -> go.Figure:
    """The previous period, dashed, moved forward onto the current period's days."""
    df = pd.DataFrame(rows)
    if df.empty or x not in df.columns or y not in df.columns:
        return fig
    xs = pd.to_datetime(df[x]) + pd.Timedelta(days=shift_days)
    fig.add_scatter(x=xs, y=df[y], mode="lines", name="Previous period", line={"dash": "dash", "color": MUTED})
    return fig


def add_markers(fig: go.Figure, markers: list[dict], series: list[dict], x: str, y: str) -> go.Figure:
    """Flagged days as dots on the trend line: one trace per severity, each with its own colour AND symbol."""
    on_day = {str(r.get(x))[:10]: r.get(y) for r in series}
    for sev in ("MODERATE", "SEVERE", "CRITICAL"):
        pts = [m for m in markers if m.get("severity") == sev and on_day.get(str(m.get("date"))[:10]) is not None]
        if not pts:
            continue
        fig.add_scatter(
            x=[str(p["date"])[:10] for p in pts], y=[on_day[str(p["date"])[:10]] for p in pts], mode="markers",
            name=sev.title(), marker={"color": SEVERITY[sev], "symbol": SYMBOLS[sev], "size": 11},
            hovertext=[f"{p.get('campaign_name')} ({p.get('platform')}): {fmt_currency(p.get('observed_cpa'))} "
                       f"per acquisition vs usual {fmt_currency(p.get('usual_cpa'))}" for p in pts],
            hoverinfo="text")
    return fig


def pacing_bullets(rows: list[dict], colors: dict | None = None) -> go.Figure | None:
    """Per platform with a budget: the month-end projection (light), spent so far (the platform's colour) and the
    budget (a tick)."""
    rows = [r for r in rows if r.get("budget")]
    if not rows:
        return None
    names = [r["platform"] for r in rows]
    fig = go.Figure()
    fig.add_bar(y=names, x=[r.get("projected") or 0 for r in rows], orientation="h", name="Month-end projection",
                marker_color=GRID)
    fig.add_bar(y=names, x=[r["spent_mtd"] for r in rows], orientation="h", name="Spent so far",
                marker_color=[platform_colour(colors, n) for n in names] if colors else COLORS["brand"], width=0.35)
    fig.add_scatter(y=names, x=[r["budget"] for r in rows], mode="markers", name="Budget",
                    marker={"symbol": "line-ns-open", "size": 28, "color": "#2b2622", "line": {"width": 3}})
    fig.update_layout(barmode="overlay")
    style(fig, {"v": "currency"}, x="v")
    fig.update_xaxes(title_text="")
    return fig
