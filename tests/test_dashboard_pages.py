"""Every page, run headless with streamlit.testing AppTest against the real API app (DuckDB, no model)."""

from pathlib import Path

import pytest
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


PAGES = ["Home.py", "pages/1_Channel_Deep_Dive.py", "pages/2_AI_Insights.py", "pages/3_Chat.py"]


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_without_an_exception(dash_api, page):
    at = run(page)
    assert not at.exception, at.exception


def test_overview_shows_kpis_pacing_and_panels(dash_api):
    at = run("Home.py")
    assert [m.label for m in at.metric] == ["Spend", "Conversions", "Cost per acquisition", "Click-through rate",
                                           "ROAS (Google)"]
    assert at.metric[0].value.startswith("$")
    text = " ".join(m.value for m in at.markdown)
    assert "Daily spend by platform" in text and "Month-end pacing" in text


def test_deep_dive_for_google_shows_google_panels_and_filters(dash_api):
    at = run("pages/1_Channel_Deep_Dive.py", dd_platform="Google")
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Average quality score" in text and "Video completion funnel" not in text
    assert "Quality score (1-10)" in [s.label for s in at.slider]


def test_switching_platform_drops_a_campaign_from_the_old_one(dash_api):
    """Review focus 2: a stale downstream choice must not raise StreamlitAPIException."""
    at = run("pages/1_Channel_Deep_Dive.py", dd_platform="Facebook")
    campaigns = at.multiselect(key="dd_campaign_name")
    facebook_campaign = campaigns.options[0]
    campaigns.select(facebook_campaign).run()
    at.session_state["dd_platform"] = "Google"
    at.run()
    assert not at.exception
    assert facebook_campaign not in at.multiselect(key="dd_campaign_name").value


def test_sidebar_chat_on_overview_writes_the_shared_history(dash_api):
    at = run("Home.py")
    at.chat_input[0].set_value(QUESTION).run()
    assert not at.exception
    messages = at.session_state["messages"]
    assert messages[0]["content"] == QUESTION and "answer" in messages[1]


def test_a_rejected_key_shows_a_banner_not_a_traceback(dash_api, monkeypatch):
    from lib import config

    monkeypatch.setattr(config, "api_key", lambda: "x" * 32)
    at = run("Home.py")
    assert not at.exception
    assert "API key was rejected" in at.error[0].value
