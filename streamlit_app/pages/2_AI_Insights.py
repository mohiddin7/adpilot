"""AI insights: the pack's standing questions (dashboard.insights), each answered by the analyst on demand over the
last 30 days of data."""

from datetime import date

import streamlit as st
from lib import chat, view
from lib.api_client import ApiError
from lib.controls import context_line, preset_range

view.start("AI insights", "💡")
m = view.guarded(view.meta)
st.title("AI insights")
start, end = preset_range("Last 30 days", date.fromisoformat(m["date_min"]), date.fromisoformat(m["date_max"]))
context = context_line("AI insights", start, end, {})
chat.sidebar(context, "insights")
st.caption(f"Each card asks the analyst one question about {start} to {end}. Free models answer, so each takes a while.")

answers = st.session_state.setdefault("insight_answers", {})
if not m["insights"]:
    st.info("This pack defines no insight questions (`dashboard.insights` in pack.yaml).")
ask_all = bool(m["insights"]) and st.button("Ask all", type="primary")
for i, question in enumerate(m["insights"]):
    with st.container(border=True):
        st.markdown(f"**{question}**")
        if ask_all or st.button("Ask", key=f"insight_{i}"):
            try:
                answers[question] = chat.run_ask(question, context, "insights")
            except ApiError as exc:
                view.show_error(exc)
        if question in answers:
            chat.render_answer(answers[question])
