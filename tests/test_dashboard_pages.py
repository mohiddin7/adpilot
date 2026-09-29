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
    messages = at.session_state["messages_chat"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "pre-defined query" in messages[1]["answer"]["answer_md"]


def test_show_thinking_streams_and_still_answers(dash_api):
    at = run("pages/3_Chat.py", show_thinking=True)
    at.chat_input[0].set_value(QUESTION).run()
    assert not at.exception
    assert at.session_state["messages_chat"][-1]["answer"]["answer_md"]


def test_a_new_conversation_forgets_the_old_one(dash_api):
    at = run("pages/3_Chat.py")
    at.chat_input[0].set_value(QUESTION).run()
    first_session = at.session_state["session_id_chat"]
    at.button(key="new_chat").click().run()
    assert not at.exception
    assert at.session_state["messages_chat"] == []
    at.chat_input[0].set_value(QUESTION).run()  # the next question starts a new server-side session
    assert at.session_state["session_id_chat"] != first_session


PAGES = ["Home.py", "pages/1_Channel_Deep_Dive.py", "pages/2_AI_Insights.py", "pages/3_Chat.py"]


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_without_an_exception(dash_api, page):
    at = run(page)
    assert not at.exception, at.exception


def test_overview_shows_kpis_pacing_and_panels(dash_api):
    at = run("Home.py")
    assert [m.label for m in at.metric] == ["Spend", "Conversions", "Cost per acquisition", "ROAS (Google)",
                                           "Click-through rate", "Cvr"]
    assert at.metric[0].value.startswith("$")
    text = " ".join(m.value for m in at.markdown)
    assert "Daily trend" in text and "Month-end pacing" in text


def test_deep_dive_for_google_shows_google_panels_and_filters(dash_api):
    at = run("pages/1_Channel_Deep_Dive.py", dd_platform="Google")
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Spend by quality score" in text and "Video completion funnel" not in text
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


def test_page_chats_are_independent(dash_api):
    at = run("Home.py")
    at.chat_input[0].set_value(QUESTION).run()
    assert not at.exception
    overview = at.session_state["messages_overview"]
    assert overview[0]["content"] == QUESTION and "answer" in overview[1]
    assert "messages_chat" not in at.session_state or at.session_state["messages_chat"] == []
    at.switch_page("pages/3_Chat.py").run()
    assert not at.exception
    assert not any(QUESTION in m.value for m in at.markdown)  # the Chat page does not show the Overview's thread
    at.chat_input[0].set_value("Which platform had the best CPA?").run()
    assert len(at.session_state["messages_chat"]) == 2 and len(at.session_state["messages_overview"]) == 2
    assert at.session_state["session_id_chat"] != at.session_state["session_id_overview"]


def test_the_sidebar_shows_the_whole_thread_and_clears_it(dash_api):
    at = run("Home.py")
    at.chat_input[0].set_value(QUESTION).run()
    at.chat_input[0].set_value("And conversions?").run()
    assert not at.exception
    assert sum(QUESTION in m.value or "And conversions?" in m.value for m in at.sidebar.markdown) == 2
    at.sidebar.button(key="clear_overview").click().run()
    assert at.session_state["messages_overview"] == []


def test_a_rejected_key_shows_a_banner_not_a_traceback(dash_api, monkeypatch):
    from lib import config

    monkeypatch.setattr(config, "api_key", lambda: "x" * 32)
    at = run("Home.py")
    assert not at.exception
    assert "API key was rejected" in at.error[0].value


def test_a_slider_the_viewer_never_moved_follows_widening_bounds(dash_api):
    """Review fix: switching platform back to All widens bounds; a slider the viewer never touched must
    follow the new bounds, not stay clamped to the old narrower range and silently drop rows."""
    at = run("pages/1_Channel_Deep_Dive.py", dd_platform="Google")
    at.session_state["dd_platform"] = "All"
    at.run()
    assert not at.exception
    for s in at.slider:
        assert s.value == (s.min, s.max)


def test_deselecting_the_platform_does_not_crash_the_deep_dive(dash_api):
    """Clicking the already-selected segment sets it to None; the page must still draw."""
    at = run("pages/1_Channel_Deep_Dive.py")
    at.session_state["dd_platform"] = None
    at.run()
    assert not at.exception, at.exception


def test_a_failed_filter_option_query_keeps_the_choice_and_says_why(dash_api, monkeypatch):
    import streamlit as st

    ac, _, _ = dash_api
    at = run("pages/1_Channel_Deep_Dive.py", dd_platform="Facebook")
    campaign = at.multiselect(key="dd_campaign_name").options[0]
    at.multiselect(key="dd_campaign_name").select(campaign).run()
    real = ac.filter_options

    def broken(page, params):
        out = real(page, params)
        out["campaign_name"] = {"values": [], "error": "SqlPolicy: campaign options could not be read"}
        out["spend"] = {"error": "SqlPolicy: spend bounds could not be read"}
        return out

    monkeypatch.setattr(ac, "filter_options", broken)
    st.cache_data.clear()
    at.run()
    assert not at.exception, at.exception
    assert at.multiselect(key="dd_campaign_name").value == [campaign]
    captions = [c.value for c in at.caption]
    assert any("campaign options could not be read" in c for c in captions)
    assert any("spend bounds could not be read" in c for c in captions)


def test_needs_attention_opens_the_flagged_campaign_in_the_deep_dive(dash_api, monkeypatch):
    """DuckDB's anomalies table is empty, so one flagged row is injected into the overview's anomalies panel."""
    from lib import view

    ac, _, _ = dash_api
    campaign = ac.filter_options("deep_dive", [("date_from", "2024-01-01"), ("date_to", "2024-01-30"),
                                              ("platform", "Google")])["campaign_name"]["values"][0]
    real = view.panels

    def flagged(page, params):
        out = real(page, params)
        for p in out:
            if p["table"] == "anomalies":
                p["rows"] = [{"date": "2024-01-30", "platform": "Google", "campaign_name": campaign,
                              "observed_cpa": 90.0, "usual_cpa": 30.0, "severity": "SEVERE", "confidence": "high"}]
        return out

    monkeypatch.setattr(view, "panels", flagged)
    at = run("Home.py")
    assert not at.exception, at.exception
    at.button(key="open_flagged").click().run()
    assert not at.exception, at.exception
    assert at.session_state["dd_platform"] == "Google"
    assert at.session_state["dd_campaign_name"] == [campaign]
    assert at.multiselect(key="dd_campaign_name").value == [campaign]  # AppTest followed switch_page


@pytest.mark.parametrize("page,preset_key", [("Home.py", "ov_preset"), ("pages/1_Channel_Deep_Dive.py", "dd_preset")])
def test_a_failed_comparison_fetch_drops_the_deltas_not_the_page(dash_api, monkeypatch, page, preset_key):
    """Last 7 days: its comparison week is inside the demo month, so a working fetch would show deltas."""
    from lib import view
    from lib.api_client import ApiError

    real = view.panels

    def prior_fails(page_id, params):
        if any(k == "panel" for k, _ in params):  # only the comparison-period call asks for named panels
            raise ApiError("server", "The AdPilot service failed (HTTP 500).")
        return real(page_id, params)

    assert any(m.delta for m in run(page, **{preset_key: "Last 7 days"}).metric)  # deltas when the fetch works
    monkeypatch.setattr(view, "panels", prior_fails)
    at = run(page, **{preset_key: "Last 7 days"})
    assert not at.exception, at.exception
    assert at.metric and not any(m.delta for m in at.metric)


def test_the_chat_page_shows_the_viewers_dollar_amounts_literally(dash_api):
    at = run("pages/3_Chat.py")
    at.chat_input[0].set_value("Why did spend go from $5K to $7K?").run()
    assert any("\\$5K to \\$7K" in m.value for m in at.markdown)
    at.run()  # the history replay path
    assert any("\\$5K to \\$7K" in m.value for m in at.markdown)


def test_a_failed_sidebar_ask_shows_its_error_once(dash_api, monkeypatch):
    from lib import api_client
    from lib.api_client import ApiError

    def fails(question, session_id):
        raise ApiError("server", "The analyst hit a snag.")

    monkeypatch.setattr(api_client, "ask", fails)
    at = run("Home.py")
    at.chat_input[0].set_value(QUESTION).run()
    assert not at.exception
    shown = [e.value for e in at.error] + [c.value for c in at.caption]
    assert shown.count("The analyst hit a snag.") == 1
