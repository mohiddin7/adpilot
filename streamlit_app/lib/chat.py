"""Chats. Each data page's sidebar has its own conversation (history `messages_<page>`, server session
`session_id_<page>`), and the Chat page has its own (`chat`). "Show thinking" is one preference for all of them."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import streamlit as st
import streamlit.components.v1 as components

from . import api_client
from . import history as saved_chats
from .api_client import ApiError
from .charts import build_figure
from .controls import answer_md, caveat_text, md, with_context
from .formatters import format_for
from .view import column_config, show_error

PHASES = {
    "thinking": "Thinking…",
    "tool": "Looking something up…",
    "sql": "Running a query…",
    "repair": "Fixing its query…",
    "answering": "Writing the answer…",
}


def session_id(page: str) -> str:
    return st.session_state.setdefault(f"session_id_{page}", uuid4().hex)


def history(page: str) -> list[dict]:
    return st.session_state.setdefault(f"messages_{page}", [])


def clear(page: str) -> None:
    st.session_state.pop(f"messages_{page}", None)
    st.session_state.pop(f"session_id_{page}", None)


def thinking_toggle() -> None:
    """On every page. The choice lives in a plain session key: Streamlit drops a widget's own state when the viewer
    switches pages."""

    def keep() -> None:
        st.session_state["show_thinking"] = st.session_state["_show_thinking"]

    st.sidebar.toggle(
        "Show thinking", value=st.session_state.get("show_thinking", False), key="_show_thinking", on_change=keep,
        help="Stream each step (query, repair, answer) while the analyst works.",
    )


def run_ask(question: str, context: str, page: str) -> dict:
    """Blocking /ask, or /ask/stream with live steps when "Show thinking" is on. Callers never know which ran."""
    sent = with_context(question, context)
    if not st.session_state.get("show_thinking"):
        with st.spinner("Thinking…"):
            return api_client.ask(sent, session_id(page))
    answer = None
    with st.status(PHASES["thinking"], expanded=True) as box:
        try:
            for name, payload in api_client.ask_stream(sent, session_id(page)):
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


def render_answer(answer: dict, compact: bool = False, colors: dict | None = None, single: str | None = None,
                  key: str | None = None) -> None:
    """`colors`: the platform colours, so a chart by platform matches the rest of the dashboard. `single`: the colour
    of the one platform in view, for a chart with a single series. `key`: unique per turn on the page."""
    st.markdown(answer_md(answer.get("answer_md") or ""))
    chart, data = answer.get("chart"), answer.get("data")
    numeric = {c for r in data or [] for c, v in r.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    formats = {c: format_for(c) for c in numeric}
    if chart and data:
        fig = build_figure(chart, data, formats, colors, single)
        if fig is not None:
            st.plotly_chart(fig, key=f"answer_{key or answer.get('trace_id')}_{'s' if compact else 'm'}")
    elif data and not compact:
        st.dataframe(data, hide_index=True, column_config=column_config(formats))
    for caveat in answer.get("caveats") or []:
        st.caption(f"Note: {md(caveat_text(caveat))}")
    if answer.get("sql") and not compact:
        with st.expander("SQL"):
            st.code(answer["sql"], language="sql")


def ask_and_record(question: str, context: str, page: str) -> dict | None:
    """Ask, keep both turns in this page's history, and show the reason on failure. Returns the answer or None."""
    turns = history(page)
    turns.append({"role": "user", "content": question})
    try:
        answer = run_ask(question, context, page)
    except ApiError as exc:
        turns.append({"role": "assistant", "content": exc.message, "error": exc.kind})
        show_error(exc)
        return None
    turns.append({"role": "assistant", "content": answer.get("answer_md") or "", "answer": answer})
    return answer


def _thread(page: str, colors: dict | None, single: str | None) -> None:
    for i, m in enumerate(history(page)):
        if m["role"] == "user":
            st.markdown(f"**You:** {md(m['content'])}")
        elif "answer" in m:
            render_answer(m["answer"], compact=True, colors=colors, single=single, key=f"{page}_{i}")
        else:
            st.caption(md(m["content"]))


def sidebar(context: str, page: str, suggestions: list[str] | tuple = (), colors: dict | None = None,
            single: str | None = None) -> None:
    """This page's own chat: its whole thread (compact), suggestions, a box and a Clear button."""
    thinking_toggle()
    with st.sidebar:
        st.subheader("Ask about this view")
        _thread(page, colors, single)
        clicked = [s for i, s in enumerate(suggestions) if st.button(s, key=f"suggest_{page}_{i}")]  # draw them all
        question = st.chat_input("Ask a question…", key=f"sidebar_chat_{page}") or (clicked[0] if clicked else None)
        if question:
            st.markdown(f"**You:** {md(question)}")
            answer = ask_and_record(question, context, page)
            if answer is not None:
                render_answer(answer, compact=True, colors=colors, single=single)
        if history(page) and st.button("Clear", key=f"clear_{page}"):
            clear(page)
            st.rerun()


_component = components.declare_component(
    "local_history", path=str(Path(__file__).resolve().parents[1] / "components" / "local_history"))


def _browser(save: str | None):
    """This browser's saved list as a JSON string ("" when empty), {"unavailable": True} when it can't keep one,
    or None until the component has answered. A component failure reads as unavailable: the chat never depends on it."""
    try:
        return _component(save=save, key="local_history", default=None)
    except Exception:  # noqa: BLE001 — saved chats are a convenience
        return {"unavailable": True}


def saved() -> list[dict] | None:
    """The Chat page's saved conversations, newest first; None until the browser's copy has arrived."""
    return st.session_state.get("chat_saved")


def restore(conv: dict) -> None:
    """Show a saved conversation and continue its server session (a fresh one if the API has forgotten it)."""
    st.session_state["messages_chat"] = [dict(t) for t in conv["turns"]]
    st.session_state["session_id_chat"] = conv["id"]


def short_date(day: str) -> str:
    """'2026-10-05' -> 'Oct 5'; anything else as stored."""
    try:
        return date.fromisoformat(day).strftime("%b %-d")
    except ValueError:
        return day


def forget(conv_id: str) -> None:
    st.session_state["chat_saved"] = saved_chats.forget(saved() or [], conv_id)
    if st.session_state.get("session_id_chat") == conv_id:
        clear("chat")


def remember_current() -> None:
    """The open conversation goes first in the saved list (once the list has loaded)."""
    if saved() is not None and history("chat"):
        st.session_state["chat_saved"] = saved_chats.remember(saved(), session_id("chat"), history("chat"),
                                                              date.today().isoformat())


# ponytail: two tabs on the Chat page, the last to save wins; merge by id if anyone needs both.
def sync_browser() -> None:
    """The last call on the Chat page, so the browser gets every change made above it. Nothing is written before the
    browser's copy arrives; when it does, it is loaded (plus this tab's open conversation) and the list redraws."""
    kept = saved()
    got = _browser(None if kept is None else saved_chats.dumps(kept))
    if kept is not None or got is None:
        return
    if isinstance(got, dict):
        st.session_state["chat_saved_unavailable"] = True
        return
    loaded = saved_chats.parse(got)
    if history("chat"):
        loaded = saved_chats.remember(loaded, session_id("chat"), history("chat"), date.today().isoformat())
    st.session_state["chat_saved"] = loaded
    st.rerun()
