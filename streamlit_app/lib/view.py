"""What every page does first, the error banners, and the cached API reads the pages share."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from . import api_client
from .api_client import ApiError
from .charts import add_markers, add_prior, build_figure, pacing_bullets
from .controls import (
    LOWER_IS_BETTER,
    MAX_RANGE_DAYS,
    PRESETS,
    applies_to,
    clamp_pair,
    clamp_range,
    delta_pct,
    distinct_names,
    keep_valid,
    md,
    no_sales_line,
    preset_range,
    prior_range,
    to_params,
    with_dates,
)
from .formatters import fmt, fmt_currency, label
from .glossary import METRIC_DEFINITIONS
from .page_style import inject_page_style
from .theme import SEVERITY, SYMBOLS, platform_colors


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
    """Every panel's campaigns told apart by name (distinct_names), once, for every chart and table that shows them."""
    return [{**p, "rows": distinct_names(p["rows"])} for p in api_client.panels(page, list(params))]


@st.cache_data(ttl=300, max_entries=200, show_spinner=False)
def pacing() -> dict:
    return api_client.pacing()


@st.cache_data(ttl=300, max_entries=100, show_spinner=False)
def insights(params: tuple[tuple[str, str], ...]) -> dict:
    return api_client.insights(list(params))


def column_config(formats: dict) -> dict:
    """Tables formatted from the panel's `formats`: dollars with separators, percent with 2 decimals, ROAS as 1.79x."""
    kinds = {"currency": "dollar", "percent": "percent", "multiple": "%.2fx", "number": "localized"}
    return {c: st.column_config.NumberColumn(label(c), format=kinds[k]) for c, k in formats.items() if k in kinds}


KPI_HELP = {"cpa": METRIC_DEFINITIONS.get("CPA"), "ctr": METRIC_DEFINITIONS.get("CTR"),
            "roas_google": METRIC_DEFINITIONS.get("ROAS")}


def date_controls(m: dict, key: str) -> tuple[date, date]:
    """A page can preset the range (session_state[f"{key}_preset"]/f"{key}_custom") before calling this: passing
    index/value only when the key is unset avoids Streamlit's "widget created with a default value but also has its
    value set via the Session State API" warning."""
    dmin, dmax = date.fromisoformat(m["date_min"]), date.fromisoformat(m["date_max"])
    preset_kwargs = {} if f"{key}_preset" in st.session_state else {"index": PRESETS.index("Last 30 days")}
    preset = st.selectbox("Date range", PRESETS, key=f"{key}_preset", **preset_kwargs)
    if preset == "Custom":
        custom_kwargs = {} if f"{key}_custom" in st.session_state else \
            {"value": (max(dmin, dmax - timedelta(days=29)), dmax)}
        picked = st.date_input("From / to", min_value=dmin, max_value=dmax, key=f"{key}_custom", **custom_kwargs)
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


PRIOR_ROLES = ("kpi", "kpi_series")


def by_role(results: list[dict], role: str) -> list[dict]:
    return [p for p in results if p.get("role") == role]


def one(results: list[dict], role: str) -> dict | None:
    return next(iter(by_role(results, role)), None)


def prior_panels(page: str, m: dict, params: list, start: date, end: date) -> dict[str, dict]:
    """The previous period of the same length for the KPI and series panels. A failure drops the deltas and
    the dashed lines, never the page."""
    ids = [("panel", p["id"]) for p in m["panels"][page] if p.get("role") in PRIOR_ROLES]
    if not ids:
        return {}
    ps, pe = prior_range(start, end)
    try:
        out = {p["id"]: p for p in panels(page, tuple(with_dates(params, ps, pe) + ids))}
    except ApiError:
        return {}
    st.caption(f"Changes compare with {ps} to {pe}.")
    return out


def drawable(p: dict | None) -> bool:
    if p is None:
        return False
    if p["error"]:
        st.error(p["error"])
        return False
    if not p["rows"]:
        st.caption(p["note"] or "No rows for this selection.")
        return False
    return True


def kpi_strip(results: list[dict], prior: dict[str, dict]) -> None:
    """One tile per KPI column: the value, the change against the previous period (an arrow and a sign; red or green
    by whether up is good) and a sparkline from the page's kpi_series panel."""
    series = one(results, "kpi_series")
    spark_rows = series["rows"] if series and not series["error"] else []
    for p in by_role(results, "kpi"):
        if p["error"]:
            st.error(f"{p['title']}: {p['error']}")
            continue
        now = p["rows"][0] if p["rows"] else {}
        pp = prior.get(p["id"])
        before = pp["rows"][0] if pp and not pp["error"] and pp["rows"] else {}
        for col, field in zip(st.columns(len(p["columns"])), p["columns"], strict=True):
            d = delta_pct(now.get(field), before.get(field)) if before else None
            spark = [r[field] for r in spark_rows if r.get(field) is not None]
            col.metric(label(field), fmt(now.get(field), p["formats"].get(field)), None if d is None else f"{d:+.1f}%",
                       delta_color="inverse" if field in LOWER_IS_BETTER else "normal", help=KPI_HELP.get(field),
                       border=True, chart_data=spark if len(spark) > 1 else None)


def trend_card(p: dict | None, colors: dict, prior: dict[str, dict], metrics: tuple[str, ...], key: str,
               shift_days: int, markers: dict | None = None, single: str | None = None) -> None:
    if p is None:
        return
    with st.container(border=True):
        st.markdown(f"**{p['title']}**")
        if not drawable(p):
            return
        options = [c for c in metrics if c in p["columns"]]
        y = st.selectbox("Metric", options, format_func=label, key=key) if len(options) > 1 else options[0]
        fig = build_figure({**p["chart"], "y": y}, p["rows"], p["formats"], colors, single)
        if fig is None:
            return
        pp = prior.get(p["id"])
        if pp and not pp["error"]:
            add_prior(fig, pp["rows"], p["chart"]["x"], y, shift_days)
        if markers and not markers["error"]:
            add_markers(fig, markers["rows"], p["rows"], p["chart"]["x"], y)
        st.plotly_chart(fig, key=f"fig_{key}")


def chart_card(p: dict | None, colors: dict, key: str | None = None, pick_z: bool = False,
               single: str | None = None) -> None:
    """`single`: the colour of the one platform in view, for a chart with a single series."""
    if p is None:
        return
    with st.container(border=True):
        st.markdown(f"**{p['title']}**")
        if not drawable(p):
            return
        chart = dict(p["chart"])
        if pick_z:
            zs = [c for c in p["columns"] if c in p["formats"] and c not in (chart["x"], chart["y"])]
            chart["z"] = st.selectbox("Metric", zs, format_func=label, key=f"z_{p['id']}") if len(zs) > 1 else chart["z"]
        rows = p["rows"]
        if p.get("role") == "map":  # the map can't place a campaign with no sales; name them under it instead
            rows = [r for r in rows if (r.get("conversions") or 0) > 0]
        fig = build_figure(chart, rows, p["formats"], colors, single)
        if fig is not None:
            st.plotly_chart(fig, key=key or f"fig_{p['id']}")
        if p.get("role") == "map" and (line := no_sales_line(p["rows"])):
            st.caption(line)
        if p["truncated"]:
            st.caption("Showing the first rows only. Narrow the dates or filters to see the rest.")


HIDDEN = {"campaign_id": None, "campaign_name_raw": None}  # keys, not for reading


def table_card(p: dict | None, flagged: frozenset[tuple[str, str]] = frozenset()) -> None:
    """A formatted table; campaigns in the flag set get a text label (colour is never the only signal)."""
    if p is None:
        return
    st.markdown(f"**{p['title']}**")
    if not drawable(p):
        return
    df = pd.DataFrame(p["rows"])
    if flagged and {"platform", "campaign_id"} <= set(df.columns):
        df.insert(0, "flag", ["⚠ flagged" if k in flagged else "" for k in zip(df["platform"], df["campaign_id"], strict=True)])
    st.dataframe(df, hide_index=True, column_config={**column_config(p["formats"]), **HIDDEN})


def leaderboard(p: dict | None, colors: dict) -> None:
    """Top 15 campaigns by the chosen metric; "best" is the lowest for cost metrics, the highest otherwise."""
    with st.container(border=True):
        st.markdown("**Campaign leaderboard**")
        if not drawable(p):
            return
        left, right = st.columns(2)
        metrics = [c for c in ("spend", "conversions", "cpa", "ctr", "roas") if c in p["columns"]]
        metric = left.selectbox("Rank by", metrics, format_func=label, key="dd_lb_metric")
        worst = right.segmented_control("Show", ["Best", "Worst"], default="Best", key="dd_lb_side") == "Worst"
        rows = sorted((r for r in p["rows"] if r.get(metric) is not None), key=lambda r: r[metric],
                      reverse=(metric in LOWER_IS_BETTER) == worst)[:15]
        fig = build_figure({"chart_type": "bar_h", "x": "campaign_name", "y": metric, "color": "platform"}, rows,
                           p["formats"], colors)
        if fig is not None:
            st.plotly_chart(fig, key="fig_leaderboard")


def efficiency_map(details: list[dict], colors: dict) -> None:
    with st.container(border=True):
        st.markdown("**Efficiency map** (spend vs cost per acquisition, sized by conversions)")
        if not details:
            return
        level = st.segmented_control("Level", ["Campaigns", "Ad sets"], default="Campaigns", key="dd_map_level")
        p = details[1] if level == "Ad sets" and len(details) > 1 else details[0]
        if not drawable(p):
            return
        rows = [r for r in p["rows"] if r.get("cpa") is not None and (r.get("conversions") or 0) > 0]
        fig = build_figure({"chart_type": "bubble", "x": "spend", "y": "cpa", "size": "conversions", "color": "platform",
                            "reference": "mean_y"}, rows, p["formats"], colors)
        if fig is not None:
            st.plotly_chart(fig, key="fig_dd_map")
        if line := no_sales_line(p["rows"]):
            st.caption(line)


def timeline(p: dict | None) -> None:
    """Flagged days per campaign over time, coloured and shaped by severity."""
    with st.container(border=True):
        st.markdown("**Anomaly timeline**")
        if not drawable(p):
            return
        fig = build_figure(p["chart"], p["rows"], p["formats"], SEVERITY)
        if fig is not None:
            fig.update_traces(marker={"size": 11})
            for trace in fig.data:
                trace.marker.symbol = SYMBOLS.get(trace.name, "circle")
            st.plotly_chart(fig, key="fig_timeline")


def flag_set(p: dict | None) -> frozenset[tuple[str, str]]:
    """Spec 3.1.5: the flag set is the attention panel's campaigns, by (platform, campaign id), and nothing else."""
    if not p or p["error"]:
        return frozenset()
    return frozenset((r.get("platform"), r["campaign_id"]) for r in p["rows"] if r.get("campaign_id"))


def attention_list(p: dict | None, start: date, end: date, limit: int = 5) -> None:
    if p is None:
        return
    with st.container(border=True):
        st.markdown(f"**{p['title']}**")
        st.caption("Campaigns with a SEVERE or CRITICAL cost-per-acquisition day in the last 7 days of data, "
                   "ranked by excess cost.")
        if not drawable(p):
            return
        for i, r in enumerate(p["rows"][:limit]):
            text, action = st.columns([5, 1])
            cost = r["excess_cost"] or 0
            text.markdown(f"**{md(r['campaign_name'])}** · {md(r['platform'])} · {r['worst']} · "
                          f"{r['flagged_days']} flagged day{'s' if r['flagged_days'] != 1 else ''} · "
                          f"about {md(fmt_currency(abs(cost)))} {'excess cost' if cost >= 0 else 'cheaper than usual'}")
            if action.button("Open in deep dive", key=f"open_{i}"):
                st.session_state.update({"dd_platform": r["platform"], "dd_campaign_name": [r.get("campaign_name_raw", r["campaign_name"])],
                                         "dd_preset": "Custom", "dd_custom": (start, end)})
                st.switch_page("pages/1_Channel_Deep_Dive.py")


def meta_colors() -> dict[str, str]:
    """The pack's platform colours as hex, for a page that draws nothing else from /dashboard. None when it can't be
    read: a chart then falls back to the non-platform colours instead of stopping the page."""
    try:
        return platform_colors(meta().get("colors") or {})
    except ApiError:
        return {}


def pacing_card(pc: dict, colors: dict | None = None) -> None:
    with st.container(border=True):
        st.markdown(f"**Month-end pacing** · as of {pc.get('as_of') or '—'} · whole account, filters don't apply")
        for problem in pc.get("problems") or []:
            st.caption(md(problem))
        fig = pacing_bullets(pc.get("rows") or [], colors)
        if fig is not None:
            st.plotly_chart(fig, key="fig_pacing")
        for r in pc.get("rows") or []:
            if r["budget"] is None:
                st.caption(f"{r['platform']}: no budget set")
            elif r.get("off_pct") is not None:
                st.caption(f"{md(r['platform'])}: projection {md(fmt_currency(r['projected']))} "
                           f"({r['off_pct']:+.0f}% vs budget)")


SEVERITY_BADGE = {"high": "🔴 HIGH", "medium": "🟠 MEDIUM", "low": "⚪ LOW"}


def stake_label(card: dict) -> str:
    """What a card's stake is: "at stake" (money lost), "change in spend" or "to reallocate". An API from before the
    field said "at stake" for all of them."""
    return card.get("stake_label", "at stake")


def insight_params(start: date, end: date, selected: dict[str, list[str]]) -> tuple[tuple[str, str], ...]:
    """/insights takes the dates and at most one platform (the whole account otherwise)."""
    params = [("date_from", start.isoformat()), ("date_to", end.isoformat())]
    platforms = selected.get("platform") or []
    return tuple(params + ([("platform", platforms[0])] if len(platforms) == 1 else []))


def what_changed(start: date, end: date, selected: dict[str, list[str]]) -> None:
    with st.container(border=True):
        st.markdown("**What changed**")
        try:
            out = insights(insight_params(start, end, selected))
        except ApiError as exc:
            show_error(exc)
            return
        for c in out["cards"][:3]:
            st.markdown(f"{SEVERITY_BADGE[c['severity']]} · {md(c['headline'])} "
                        f"(about {md(fmt_currency(c['stake']))} {stake_label(c)})")
        if not out["cards"]:
            failed = out["problems"] and not out["checked"]
            st.caption("Couldn't check this period." if failed else "Nothing needs attention in this period.")
        for problem in out["problems"]:
            st.caption(md(problem))
        if st.button("All findings and why →", key="all_findings"):  # the same window and platform, like "Open in deep dive"
            platforms = selected.get("platform") or []
            st.session_state.update({"ins_preset": "Custom", "ins_custom": (start, end),
                                     "ins_platform": platforms[0] if len(platforms) == 1 else "All"})
            st.switch_page("pages/2_AI_Insights.py")


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


