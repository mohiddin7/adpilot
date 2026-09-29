"""AI insights: findings the analysis engine ranked by the dollars at stake in the chosen window, each with its
evidence chart, numbers, action and confidence. "Investigate why" asks the analyst about one finding; the answer stays
under its card for the session."""

import streamlit as st
from lib import chat, view
from lib.api_client import ApiError
from lib.charts import build_figure
from lib.controls import context_line, investigate_question, md
from lib.formatters import fmt_currency
from lib.theme import platform_colors

view.start("AI insights", "💡")
m = view.guarded(view.meta)
colors = platform_colors(m.get("colors") or {})
st.title("AI insights")

segment = next((f for f in m["filters"] if f["column"] == "platform" and f.get("values")), None)
with st.container(border=True):
    left, right = st.columns([1, 2])
    with left:
        start, end = view.date_controls(m, "ins")
    with right:
        platform = st.segmented_control("Platform", ["All", *(segment["values"] if segment else [])], default="All",
                                        key="ins_platform") or "All"
selected = {} if platform == "All" else {"platform": [platform]}
context = context_line("AI insights", start, end, selected)
chat.sidebar(context, "insights", suggestions=m.get("insights") or [])

out = view.guarded(view.insights, view.insight_params(start, end, selected))
cards = out["cards"]
if cards:
    st.markdown(md(f"**{len(cards)} finding{'s' if len(cards) != 1 else ''} · about "
                   f"{fmt_currency(sum(c['stake'] for c in cards))} at stake**"))
else:
    st.markdown("**Nothing needs attention in this period.**")
    st.markdown(md("Checked: " + (", ".join(out["checked"]) or "nothing could be checked")))
for problem in out["problems"]:
    st.caption(md(problem))

investigations = st.session_state.setdefault("investigations", {})
for c in cards:
    with st.container(border=True):
        st.markdown(md(f"{view.SEVERITY_BADGE[c['severity']]} · about {fmt_currency(c['stake'])} at stake"))
        st.markdown(md(f"### {c['title']}"))
        st.markdown(md(c["headline"]))
        left, right = st.columns([3, 2])
        with left:
            chart = c.get("chart")
            fig = build_figure(chart["spec"], chart["rows"], chart["formats"], colors) if chart else None
            if fig is not None:
                st.plotly_chart(fig, key=f"card_{c['id']}")
        with right:
            if c["numbers"]:
                st.dataframe(c["numbers"], hide_index=True)
            st.markdown(md(f"**Do:** {c['action']}"))
            st.caption(md(f"Why: {c['why']}"))
            st.caption(md(f"Confidence: {c['confidence']}"))
        ask, send = st.columns(2)
        if ask.button("Investigate why", key=f"inv_{c['id']}"):
            try:
                investigations[c["id"]] = {"answer": chat.run_ask(investigate_question(c["facts"]), context, "insights")}
            except ApiError as exc:
                investigations[c["id"]] = {"error": exc.message}
        if send.button("Ask in chat", key=f"chat_{c['id']}"):
            chat.ask_and_record(investigate_question(c["facts"]), context, "insights")
            st.rerun()
        got = investigations.get(c["id"])
        if got and "answer" in got:
            with st.container(border=True):
                chat.render_answer(got["answer"])
        elif got:
            st.warning(got["error"])
