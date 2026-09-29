"""Overview: what a paid-media lead checks first. Headline numbers against the previous period, month-end pacing,
trends, what needs attention and what the optimizer would change. Every figure comes from adpilot-api."""

import streamlit as st
from lib import chat, view
from lib.api_client import ApiError
from lib.controls import context_line, prior_range, with_dates

view.start("Overview", "📊")
m = view.guarded(view.meta)
st.title("Overview")
st.caption(f"Data available {m['date_min']} to {m['date_max']}")

with st.container(border=True):
    left, right = st.columns([1, 3])
    with left:
        start, end = view.date_controls(m, "ov")
        compare = st.toggle("Compare with the previous period", value=True, key="ov_compare")
    with right:
        selected, params = view.filter_controls("overview", m, start, end, "ov", ncols=2)

chat.sidebar(context_line("Overview", start, end, selected), "overview")

results = view.guarded(view.panels, "overview", tuple(params))
prior = None
ids = view.kpi_ids(m, "overview")
if compare and ids:
    prior_start, prior_end = prior_range(start, end)
    try:
        prior_results = view.panels("overview", tuple(with_dates(params, prior_start, prior_end) + ids))
    except ApiError:
        prior_results = []  # no deltas; the page still draws
    prior = prior_results[0] if prior_results else None
    st.caption(f"Changes compare with {prior_start} to {prior_end}.")


def pacing_section() -> None:
    try:
        view.pacing_card(view.pacing())
    except ApiError as exc:
        view.show_error(exc)


view.render_page(results, prior, after_kpis=pacing_section)

attention = next((p for p in results if p["table"] == "anomalies" and p["rows"]), None)
flagged = list(dict.fromkeys((r["platform"], r["campaign_name"]) for r in attention["rows"]
                             if r.get("platform") and r.get("campaign_name"))) if attention else []
if flagged:
    pick = st.selectbox("Flagged campaign", flagged, format_func=lambda pc: f"{pc[1]} ({pc[0]})", key="ov_flagged")
    if st.button("Open in channel deep dive", key="open_flagged"):
        st.session_state["dd_platform"], st.session_state["dd_campaign_name"] = pick[0], [pick[1]]
        st.switch_page("pages/1_Channel_Deep_Dive.py")
