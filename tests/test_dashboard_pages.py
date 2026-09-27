"""Every page, run headless with streamlit.testing AppTest against the real API app (DuckDB, no model)."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_DIR = Path(__file__).resolve().parents[1] / "streamlit_app"
QUESTION = "What was spend by platform?"


def run(page: str, **state) -> AppTest:
    at = AppTest.from_file(str(APP_DIR / page), default_timeout=60)
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


def test_chat_page_answers_and_keeps_history(dash_api):
    at = run("pages/3_Chat.py")
    at.chat_input[0].set_value(QUESTION).run()
    assert not at.exception
    messages = at.session_state["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "pre-defined query" in messages[1]["answer"]["answer_md"]


def test_show_thinking_streams_and_still_answers(dash_api):
    at = run("pages/3_Chat.py", show_thinking=True)
    at.chat_input[0].set_value(QUESTION).run()
    assert not at.exception
    assert at.session_state["messages"][-1]["answer"]["answer_md"]


def test_a_new_conversation_forgets_the_old_one(dash_api):
    at = run("pages/3_Chat.py")
    at.chat_input[0].set_value(QUESTION).run()
    first_session = at.session_state["session_id"]
    at.button[0].click().run()
    assert not at.exception
    assert at.session_state["messages"] == []
    at.chat_input[0].set_value(QUESTION).run()  # the next question starts a new server-side session
    assert at.session_state["session_id"] != first_session
