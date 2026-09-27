"""One chat for the whole app: the sidebar box on every data page and the Chat page share one session id and one
history, so a question asked from the Overview sidebar continues on the Chat page."""

from __future__ import annotations

from uuid import uuid4

import streamlit as st

from . import api_client
from .api_client import ApiError
from .charts import build_figure
from .controls import md, with_context
from .view import show_error

PHASES = {
    "thinking": "Thinking…",
    "tool": "Looking something up…",
    "sql": "Running a query…",
    "repair": "Fixing its query…",
    "answering": "Writing the answer…",
}


def session_id() -> str:
    return st.session_state.setdefault("session_id", uuid4().hex)


def history() -> list[dict]:
    return st.session_state.setdefault("messages", [])


def thinking_toggle() -> None:
    """On every page. The choice lives in a plain session key: Streamlit drops a widget's own state when the viewer
    switches pages."""

    def keep() -> None:
        st.session_state["show_thinking"] = st.session_state["_show_thinking"]

    st.sidebar.toggle(
        "Show thinking", value=st.session_state.get("show_thinking", False), key="_show_thinking", on_change=keep,
        help="Stream each step (query, repair, answer) while the analyst works.",
    )


def run_ask(question: str, context: str = "") -> dict:
    """Blocking /ask, or /ask/stream with live steps when "Show thinking" is on. Callers never know which ran."""
    sent = with_context(question, context)
    if not st.session_state.get("show_thinking"):
        with st.spinner("Thinking…"):
            return api_client.ask(sent, session_id())
    answer = None
    with st.status(PHASES["thinking"], expanded=True) as box:
        try:
            for name, payload in api_client.ask_stream(sent, session_id()):
                if name == "status":
                    box.update(label=PHASES.get(payload.get("phase"), "Working…"))
                    if payload.get("phase") == "sql" and payload.get("detail"):
                        st.code(payload["detail"], language="sql")
                elif name == "answer":
                    answer = payload
        except ApiError:
            box.update(label="Stopped", state="error")
            raise
        box.update(label="Done", state="complete", expanded=False)
    if answer is None:
        raise ApiError("server", "The answer stream ended without an answer.")
    return answer


def render_answer(answer: dict, compact: bool = False) -> None:
    st.markdown(md(answer.get("answer_md") or ""))
    chart, data = answer.get("chart"), answer.get("data")
    if chart and data:
        fig = build_figure(chart, data)
        if fig is not None:
            st.plotly_chart(fig, key=f"answer_{answer.get('trace_id')}_{'s' if compact else 'm'}")
    elif data and not compact:
        st.dataframe(data, hide_index=True)
    for caveat in answer.get("caveats") or []:
        st.caption(md(f"Note: {caveat}"))
    if answer.get("sql") and not compact:
        with st.expander("SQL"):
            st.code(answer["sql"], language="sql")


def ask_and_record(question: str, context: str = "") -> dict | None:
    """Ask, keep both turns in the shared history, and show the reason on failure. Returns the answer or None."""
    turns = history()
    turns.append({"role": "user", "content": question})
    try:
        answer = run_ask(question, context)
    except ApiError as exc:
        turns.append({"role": "assistant", "content": exc.message, "error": exc.kind})
        show_error(exc)
        return None
    turns.append({"role": "assistant", "content": answer.get("answer_md") or "", "answer": answer})
    return answer


def sidebar(context: str) -> None:
    """The compact chat on every data page: the thinking toggle, a box, and the latest answer."""
    thinking_toggle()
    with st.sidebar:
        st.subheader("Ask about this view")
        question = st.chat_input("Ask a question…", key="sidebar_chat")
        if question:
            ask_and_record(question, context)
        last = next((m for m in reversed(history()) if m["role"] == "assistant"), None)
        if last is not None and "answer" in last:  # a failed ask already showed its banner
            render_answer(last["answer"], compact=True)
        st.caption("The full conversation is on the Chat page.")
