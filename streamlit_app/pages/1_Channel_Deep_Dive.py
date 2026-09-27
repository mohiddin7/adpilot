"""Channel deep dive: one platform (or all) with every filter the pack declares — campaign, ad set, anomaly
severity/confidence/direction and per-day metric ranges — plus a trend with a metric picker, the campaign and ad-set
tables with flagged campaigns marked, and the platform's own panels (quality score, frequency, video funnel)."""

import streamlit as st
from lib import chat, view
from lib.controls import context_line, prior_range, with_dates

view.start("Channel deep dive", "🎯")
m = view.guarded(view.meta)
st.title("Channel deep dive")

segment = next((f for f in m["filters"] if f["column"] == "platform" and f.get("values")), None)
platform = "All"
if segment:
    platform = st.segmented_control("Platform", ["All", *segment["values"]], default="All", key="dd_platform") or "All"
fixed = {} if platform == "All" else {"platform": [platform]}

with st.expander("Filters", expanded=True):
    left, right = st.columns([1, 3])
    with left:
        start, end = view.date_controls(m, "dd")
        compare = st.toggle("Compare with the previous period", value=True, key="dd_compare")
    with right:
        selected, params = view.filter_controls(
            "deep_dive", m, start, end, "dd", fixed=fixed, skip=frozenset({"platform"}) if segment else frozenset()
        )

chat.sidebar(context_line("Channel deep dive", start, end, selected))

results = view.guarded(view.panels, "deep_dive", tuple(params))
prior = None
ids = view.kpi_ids(m, "deep_dive")
if compare and ids:
    prior_start, prior_end = prior_range(start, end)
    prior_results = view.guarded(view.panels, "deep_dive", tuple(with_dates(params, prior_start, prior_end) + ids))
    prior = prior_results[0] if prior_results else None
    st.caption(f"Changes compare with {prior_start} to {prior_end}.")

view.render_page(results, prior)
