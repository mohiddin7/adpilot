"""Overview: the media buyer's cockpit. Headline numbers against the previous period, what changed, trends, where
the money works, mix and funnel, pacing and the optimizer, and the campaigns that need attention. Every figure comes
from adpilot-api."""

import streamlit as st
from lib import chat, view
from lib.api_client import ApiError
from lib.controls import context_line
from lib.theme import platform_colors

view.start("Overview", "📊")
m = view.guarded(view.meta)
colors = platform_colors(m.get("colors") or {})
st.title("Overview")

with st.container(border=True):
    left, middle, right = st.columns([1.2, 3, 1])
    with left:
        start, end = view.date_controls(m, "ov")
    with middle:
        selected, params = view.filter_controls("overview", m, start, end, "ov", ncols=2)
    with right:
        compare = st.toggle("Compare with the previous period", value=True, key="ov_compare")
st.caption(f"Data available {m['date_min']} to {m['date_max']}")
chat.sidebar(context_line("Overview", start, end, selected), "overview", colors=colors)

results = view.guarded(view.panels, "overview", tuple(params))
prior = view.prior_panels("overview", m, params, start, end) if compare else {}
days = (end - start).days + 1

view.kpi_strip(results, prior)
changed = st.container()  # filled last: a slow /insights never holds up the charts below

st.subheader("Trends")
left, right = st.columns(2)
with left:
    view.trend_card(view.one(results, "kpi_series"), colors, prior, ("spend", "conversions"), "ov_trend", days)
with right:
    for p in view.by_role(results, "trend"):
        view.chart_card(p, colors)

st.subheader("Where the money works")
for p in view.by_role(results, "map"):
    view.chart_card(p, colors)
left, right = st.columns(2)
with left:
    for p in view.by_role(results, "compare"):
        if p["table"] != "budget":
            view.chart_card(p, colors)
with right:
    view.chart_card(view.one(results, "funnel"), colors)

st.subheader("Budget")
left, right = st.columns(2)
with left:
    try:
        view.pacing_card(view.pacing(), colors)
    except ApiError as exc:
        view.show_error(exc)
with right:
    for p in view.by_role(results, "compare"):
        if p["table"] == "budget":
            view.chart_card(p, colors)

view.attention_list(view.one(results, "attention"), start, end)

with changed:
    view.what_changed(start, end, selected)
