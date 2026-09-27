"""What every page does first, the error banners, and the cached API reads the pages share."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from . import api_client
from .api_client import ApiError
from .charts import build_figure
from .controls import (
    LOWER_IS_BETTER,
    MAX_RANGE_DAYS,
    PRESETS,
    applies_to,
    clamp_pair,
    clamp_range,
    delta_pct,
    keep_valid,
    md,
    preset_range,
    to_params,
)
from .formatters import fmt_currency, fmt_number, fmt_pct
from .glossary import METRIC_DEFINITIONS
from .page_style import inject_page_style
from .theme import COLORS


def start(title: str, icon: str) -> None:
    st.set_page_config(page_title=f"{title} · AdPilot", page_icon=icon, layout="wide")
    inject_page_style()


def show_error(exc: ApiError) -> None:
    if exc.kind == "auth":
        st.error("The dashboard can't reach AdPilot: its API key was rejected. "
                 "The owner needs to update the dashboard's [api] secrets.")
    elif exc.kind == "rate_limited":
        st.warning(f"AdPilot is busy. Try again in {exc.retry_after} seconds.")
    elif exc.kind == "unavailable":
        st.warning("AdPilot didn't respond (it may be starting up). Try again in a minute.")
    else:
        st.error(exc.message)


def guarded(fn, *args):
    """An API read the page cannot draw without: on failure, say why and stop the page."""
    try:
        return fn(*args)
    except ApiError as exc:
        show_error(exc)
        st.stop()


# Short client-side caches: every widget click reruns the page, and the API caches for 15 minutes anyway.
# Exceptions are never cached, so a failed read is retried on the next rerun.
@st.cache_data(ttl=300, max_entries=200,
               show_spinner="Loading AdPilot… the first view after a quiet spell can take a little while to wake up.")
def meta() -> dict:
    return api_client.dashboard()


@st.cache_data(ttl=300, max_entries=200, show_spinner=False)
def options(page: str, params: tuple[tuple[str, str], ...]) -> dict:
    return api_client.filter_options(page, list(params))


@st.cache_data(ttl=300, max_entries=200, show_spinner="Loading panels…")
def panels(page: str, params: tuple[tuple[str, str], ...]) -> list[dict]:
    return api_client.panels(page, list(params))


@st.cache_data(ttl=300, max_entries=200, show_spinner=False)
def pacing() -> dict:
    return api_client.pacing()


KPI_LABELS = {"spend": "Spend", "conversions": "Conversions", "cpa": "Cost per acquisition",
              "ctr": "Click-through rate", "roas_google": "ROAS (Google)"}
KPI_FORMATS = {"spend": fmt_currency, "cpa": fmt_currency, "ctr": lambda v: fmt_pct(v, decimals=2),
               "roas_google": lambda v: "—" if v is None else f"{v:.2f}x"}
KPI_HELP = {"cpa": METRIC_DEFINITIONS.get("CPA"), "ctr": METRIC_DEFINITIONS.get("CTR"),
            "roas_google": METRIC_DEFINITIONS.get("ROAS")}


def date_controls(m: dict, key: str) -> tuple[date, date]:
    dmin, dmax = date.fromisoformat(m["date_min"]), date.fromisoformat(m["date_max"])
    preset = st.selectbox("Date range", PRESETS, index=PRESETS.index("Last 30 days"), key=f"{key}_preset")
    if preset == "Custom":
        picked = st.date_input("From / to", value=(max(dmin, dmax - timedelta(days=29)), dmax),
                               min_value=dmin, max_value=dmax, key=f"{key}_custom")
        if not isinstance(picked, (tuple, list)) or len(picked) != 2:
            st.info("Pick an end date to apply the range.")
            st.stop()
        start, end = picked
    else:
        start, end = preset_range(preset, dmin, dmax)
    start, end, clamped = clamp_range(start, end)
    if clamped:
        st.caption(f"Ranges are capped at {MAX_RANGE_DAYS} days: showing {start} to {end}.")
    return start, end


def filter_controls(page: str, m: dict, start: date, end: date, key: str,
                    fixed: dict[str, list[str]] | None = None, skip: frozenset[str] = frozenset(),
                    ncols: int = 3) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Every filter the pack declares for this page, in declaration order, cascading. Returns the categorical
    selections (for the chat context) and the API query pairs."""
    fixed = fixed or {}
    declared = [f for f in m["filters"] if page in f["pages"] and f["column"] not in skip and f["column"] not in fixed]
    cats = [f["column"] for f in declared if f["type"] == "categorical"]
    # One /filters call answers every widget. If dropping a stale choice changed an ancestor, ask once more.
    opts: dict = {}
    for _ in range(2):
        chosen = {c: list(st.session_state.get(f"{key}_{c}", [])) for c in cats}
        opts = guarded(options, page, tuple(to_params(start, end, {**fixed, **chosen})))
        pruned = False
        for c in cats:
            if "error" in opts.get(c, {}):
                continue  # options failed to load: nothing to check the choice against, so keep it
            valid = keep_valid(chosen[c], opts.get(c, {}).get("values", []))
            if valid != chosen[c]:
                st.session_state[f"{key}_{c}"] = valid
                pruned = True
        if not pruned:
            break
    platforms = fixed.get("platform") or (list(st.session_state.get(f"{key}_platform") or []) if "platform" in cats
                                          else [])
    selected: dict[str, list[str]] = dict(fixed)
    ranges: dict[str, tuple[float, float]] = {}
    bounds: dict[str, tuple[float, float]] = {}
    cols = st.columns(ncols)
    shown = 0
    for f in declared:
        if not applies_to(f.get("platforms"), platforms):
            continue
        column, option = f["column"], opts.get(f["column"], {})
        slot = cols[shown % ncols]
        state_key = f"{key}_{column}"
        if f["type"] == "categorical":
            choices = option.get("values") or st.session_state.get(state_key, [])
            selected[column] = slot.multiselect(f["label"], choices, key=state_key)
            if "error" in option:
                slot.caption(option["error"])
        elif "error" in option:
            slot.caption(f"{f['label']}: {option['error']}")
        else:
            lo, hi = option.get("min"), option.get("max")
            if lo is None or hi is None or float(lo) >= float(hi):
                continue
            lo, hi = float(lo), float(hi)
            bounds_key = f"{state_key}_bounds"
            if state_key in st.session_state:
                current = tuple(st.session_state[state_key])
                if current == st.session_state.get(bounds_key):
                    st.session_state[state_key] = (lo, hi)  # never touched: follow the new bounds
                else:
                    st.session_state[state_key] = clamp_pair(current, lo, hi)
                ranges[column] = slot.slider(f["label"], lo, hi, key=state_key)
            else:
                ranges[column] = slot.slider(f["label"], lo, hi, (lo, hi), key=state_key)
            st.session_state[bounds_key] = (lo, hi)
            bounds[column] = (lo, hi)
        shown += 1
    return selected, to_params(start, end, selected, ranges, bounds)


def kpi_ids(m: dict, page: str) -> list[tuple[str, str]]:
    """`panel=` pairs for the page's KPI panels: the comparison period needs only those."""
    return [("panel", p["id"]) for p in m["panels"][page] if p["kind"] == "kpi"]


def kpi_row(panel: dict, prior: dict | None) -> None:
    if panel["error"]:
        st.error(f"{panel['title']}: {panel['error']}")
        return
    now = panel["rows"][0] if panel["rows"] else {}
    before = prior["rows"][0] if prior and not prior["error"] and prior["rows"] else {}
    fields = panel["columns"]
    for col, field in zip(st.columns(len(fields)), fields, strict=True):
        d = delta_pct(now.get(field), before.get(field)) if before else None
        col.metric(
            KPI_LABELS.get(field, field.replace("_", " ").capitalize()),
            KPI_FORMATS.get(field, fmt_number)(now.get(field)),
            None if d is None else f"{d:+.1f}%",
            delta_color="inverse" if field in LOWER_IS_BETTER else "normal",
            help=KPI_HELP.get(field), border=True,
        )


def chart_panel(p: dict) -> None:
    """A chart card; one with several numeric columns (the daily trend) gets a metric picker."""
    y = None
    if p["chart"] and not p["error"] and p["rows"]:
        skip = {p["chart"]["x"], p["chart"].get("color")}
        numeric = [c for c in p["columns"] if c not in skip
                   and any(isinstance(r.get(c), (int, float)) for r in p["rows"])]
        if len(numeric) > 1:
            default = numeric.index(p["chart"]["y"]) if p["chart"]["y"] in numeric else 0
            y = st.selectbox("Metric", numeric, index=default, key=f"metric_{p['id']}")
    panel_card(p, y=y)


def panel_card(p: dict, y: str | None = None, flagged: frozenset[str] = frozenset()) -> None:
    with st.container(border=True):
        st.markdown(f"**{p['title']}**")
        if p["error"]:
            st.error(p["error"])
            return
        if not p["rows"]:
            st.caption(p["note"] or "No rows for this selection.")
            return
        if p["kind"] == "chart":
            fig = build_figure({**p["chart"], **({"y": y} if y else {})}, p["rows"])
            if fig is not None:
                st.plotly_chart(fig, key=f"panel_{p['id']}")
        else:
            st.dataframe(table(p["rows"], flagged), hide_index=True)
        if p["truncated"]:
            st.caption("Showing the first rows only. Narrow the dates or filters to see the rest.")


def table(rows: list[dict], flagged: frozenset[str]):
    """Flagged campaigns get a text label and a tint: colour is never the only signal (palette.json constraints)."""
    df = pd.DataFrame(rows)
    if not flagged or "campaign_name" not in df.columns:
        return df
    df.insert(0, "flag", ["⚠ flagged" if c in flagged else "" for c in df["campaign_name"]])
    tint = f"background-color: {COLORS['open']}22"
    return df.style.apply(lambda r: [tint if r["flag"] else ""] * len(r), axis=1)


def pacing_card(pc: dict) -> None:
    with st.container(border=True):
        st.markdown(f"**Month-end pacing** · as of {pc.get('as_of') or '—'} · whole account, filters don't apply")
        for problem in pc.get("problems") or []:
            st.caption(md(problem))
        for r in pc.get("rows") or []:
            if r["budget"] is None:
                st.caption(f"{r['platform']}: no budget set")
                continue
            st.progress(
                min(max(r["spent_mtd"] / r["budget"], 0.0), 1.0),
                text=md(f"{r['platform']}: {fmt_currency(r['spent_mtd'])} of {fmt_currency(r['budget'])} spent · "
                        f"month-end projection {fmt_currency(r['projected'])} ({r['off_pct']:+.0f}% vs budget)"),
            )


def render_page(results: list[dict], prior_kpi: dict | None, after_kpis=None) -> None:
    """Pack order, laid out by kind: KPI rows, then `after_kpis` (the overview's pacing card), then charts two to a
    row, then tables full width. Campaigns that appear in an anomalies panel are flagged in the other tables."""
    flagged = frozenset(r["campaign_name"] for p in results if p["table"] == "anomalies"
                        for r in p["rows"] if r.get("campaign_name"))
    for p in results:
        if p["kind"] == "kpi":
            kpi_row(p, prior_kpi)
    if after_kpis is not None:
        after_kpis()
    charts = [p for p in results if p["kind"] == "chart"]
    for i in range(0, len(charts), 2):
        for col, p in zip(st.columns(2), charts[i : i + 2], strict=False):
            with col:
                chart_panel(p)
    for p in results:
        if p["kind"] == "table":
            panel_card(p, flagged=frozenset() if p["table"] == "anomalies" else flagged)
