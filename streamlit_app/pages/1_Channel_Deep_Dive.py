"""Channel deep dive: one platform (or all) with every filter the pack declares, the KPI strip plus the platform's
own tiles, a trend explorer with flagged days marked, the campaign leaderboard, the efficiency map, the day-of-week
heatmap, the platform's own charts, the anomaly timeline and the formatted campaign details."""

import streamlit as st
from lib import chat, view
from lib.controls import context_line
from lib.theme import platform_colors, platform_colour

view.start("Channel deep dive", "🎯")
m = view.guarded(view.meta)
colors = platform_colors(m.get("colors") or {})
st.title("Channel deep dive")

segment = next((f for f in m["filters"] if f["column"] == "platform" and f.get("values")), None)
platform = "All"
if segment:
    platform = st.segmented_control("Platform", ["All", *segment["values"]], default="All", required=True,
                                     key="dd_platform") or "All"
fixed = {} if platform == "All" else {"platform": [platform]}
# one platform in view: its single-series charts wear its colour; None for "All"
single = platform_colour(colors, None if platform == "All" else platform)

with st.expander("Filters", expanded=True):
    left, right = st.columns([1, 3])
    with left:
        start, end = view.date_controls(m, "dd")
        compare = st.toggle("Compare with the previous period", value=True, key="dd_compare")
    with right:
        selected, params = view.filter_controls(
            "deep_dive", m, start, end, "dd", fixed=fixed, skip=frozenset({"platform"}) if segment else frozenset()
        )

chat.sidebar(context_line("Channel deep dive", start, end, selected), "deep_dive", colors=colors, single=single)

results = view.guarded(view.panels, "deep_dive", tuple(params))
prior = view.prior_panels("deep_dive", m, params, start, end) if compare else {}
days = (end - start).days + 1
markers = view.one(results, "markers")
details = view.by_role(results, "details")  # campaigns, then ad sets (pack order)

view.kpi_strip(results, prior)
view.trend_card(view.one(results, "kpi_series"), colors, prior,
                ("spend", "conversions", "cpa", "ctr", "cpc", "cpm", "roas_google"), "dd_trend", days, markers, single)

left, right = st.columns(2)
with left:
    view.leaderboard(details[0] if details else None, colors)
with right:
    view.efficiency_map(details, colors)

view.chart_card(view.one(results, "heatmap"), colors, pick_z=True, single=single)

own = view.by_role(results, "platform")
for i in range(0, len(own), 2):
    for col, p in zip(st.columns(2), own[i : i + 2], strict=False):
        with col:
            view.chart_card(p, colors, single=single)

view.timeline(markers)

with st.expander("Campaign details", expanded=False):
    flagged = view.flag_set(view.one(results, "attention"))
    for p in details:
        view.table_card(p, flagged)
