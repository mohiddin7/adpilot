"""AI insights: findings the analysis engine ranked (money lost first) in the chosen window, each with its evidence
chart, numbers, action and confidence. "Why this happened" opens the engine's own breakdown of the finding (no model);
"Ask in chat" sends the finding to this page's chat."""

import streamlit as st
from lib import chat, view
from lib.charts import build_figure
from lib.controls import ASK_WHY, context_line, md
from lib.formatters import fmt_currency
from lib.theme import platform_colors, platform_colour

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
chat.sidebar(context, "insights", suggestions=m.get("insights") or [], colors=colors)

params = view.insight_params(start, end, selected)
out = view.guarded(view.insights, params)
cards = out["cards"]
if cards:
    lost = out.get("at_stake") or 0  # the server's sum of the cards whose stake is money lost
    st.markdown(md(f"**{len(cards)} finding{'s' if len(cards) != 1 else ''}"
                   + (f" · about {fmt_currency(lost)} at stake" if lost else "") + "**"))
elif out["problems"] and not out["checked"]:
    st.markdown("**Couldn't check this period.**")
else:
    st.markdown("**Nothing needs attention in this period.**")
    st.markdown(md("Checked: " + (", ".join(out["checked"]) or "nothing could be checked")))
for problem in out["problems"]:
    st.caption(md(problem))

for c in cards:
    single = platform_colour(colors, c.get("platform"))  # the finding's platform colours its single-series charts
    with st.container(border=True):
        st.markdown(md(f"{view.SEVERITY_BADGE[c['severity']]} · about {fmt_currency(c['stake'])} {view.stake_label(c)}"))
        st.markdown(md(f"### {c['title']}"))
        st.markdown(md(c["headline"]))
        for sentence in c.get("also") or []:  # the same campaign's other findings, folded into this card
            st.markdown(md(f"Also: {sentence}"))
        left, right = st.columns([3, 2])
        with left:
            chart = c.get("chart")
            fig = build_figure(chart["spec"], chart["rows"], chart["formats"], colors, single) if chart else None
            if fig is not None:
                st.plotly_chart(fig, key=f"card_{c['id']}")
        with right:
            if c["numbers"]:
                st.dataframe(c["numbers"], hide_index=True)
            st.markdown(md(f"**Do:** {c['action']}"))
            st.caption(md(f"Why: {c['why']}"))
            st.caption(md(f"Confidence: {c['confidence']}"))
        why = c.get("why_detail")  # the engine's breakdown; None for a budget move, absent on an older API
        if why:
            with st.expander("Why this happened"):
                st.markdown(md(why["text"]))
                detail = why.get("chart")
                fig = build_figure(detail["spec"], detail["rows"], detail["formats"], colors, single) if detail else None
                if fig is not None:
                    st.plotly_chart(fig, key=f"why_{c['id']}")
        if st.button("Ask in chat", key=f"chat_{c['id']}"):
            chat.ask_and_record(ASK_WHY + c["facts"], context, "insights")
            st.rerun()
