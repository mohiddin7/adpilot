"""Every page, run headless with streamlit.testing AppTest against the real API app (DuckDB, no model)."""

import json
from datetime import date
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


def test_overview_leads_with_kpis_and_charts(dash_api):
    at = run("Home.py")
    assert not at.exception, at.exception
    assert [m.label for m in at.metric] == ["Spend", "Conversions", "Cost per acquisition", "ROAS (Google)",
                                           "Click-through rate", "Conversion rate"]
    assert at.metric[0].value.startswith("$") and at.metric[4].value.endswith("%")
    text = " ".join(m.value for m in at.markdown)
    for title in ("Daily trend", "Cost per acquisition by platform", "Efficiency map", "Share of spend vs share of conversions",
                  "Funnel", "Month-end pacing", "Needs attention", "What changed"):
        assert title in text, title
    assert not at.dataframe  # chart-first: no table on the Overview


def test_compare_at_the_start_of_the_data_draws_without_deltas(dash_api):
    """Review focus 2: the comparison period lies before date_min, so there is nothing to compare against."""
    at = run("Home.py", ov_preset="Custom", ov_custom=(date(2024, 1, 1), date(2024, 1, 7)))
    assert not at.exception, at.exception
    assert at.metric and not any(m.delta for m in at.metric)


@pytest.mark.parametrize("platform,has,lacks", [
    ("Google", ["Spend by quality score", "Search impression share", "Quality score"], ["Video completion funnel"]),
    ("Facebook", ["Ad fatigue", "Frequency"], ["Spend by quality score"]),
    ("TikTok", ["Video completion funnel"], ["Ad fatigue"]),
    ("All", ["Trend explorer", "Campaign leaderboard", "Efficiency map", "Day of week", "Anomaly timeline"], ["Ad fatigue"]),
])
def test_deep_dive_per_platform(dash_api, platform, has, lacks):
    """Review focus 3: one platform in play still draws every section."""
    at = run("pages/1_Channel_Deep_Dive.py", dd_platform=platform)
    assert not at.exception, at.exception
    text = " ".join([m.value for m in at.markdown] + [m.label for m in at.metric])
    assert all(h in text for h in has), [h for h in has if h not in text]
    assert not any(x in text for x in lacks)


def test_the_leaderboard_ranks_best_and_worst(dash_api):
    at = run("pages/1_Channel_Deep_Dive.py")
    at.selectbox(key="dd_lb_metric").set_value("cpa").run()
    assert not at.exception
    at.session_state["dd_lb_side"] = "Worst"  # a segmented control is set through its state key, as dd_platform is
    at.run()
    assert not at.exception


def test_campaign_details_are_formatted_and_flagged(dash_api, monkeypatch):
    campaign = _flag(monkeypatch, dash_api)
    at = run("pages/1_Channel_Deep_Dive.py")
    assert not at.exception, at.exception
    df = at.dataframe[0].value
    assert "flag" in df.columns and set(df.loc[df["campaign_name"] == campaign, "flag"]) == {"⚠ flagged"}
    assert set(df.loc[df["campaign_name"] != campaign, "flag"]) <= {""}  # only the attention panel's rows are flagged


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


def _flag(monkeypatch, dash_api, platform="Google", excess_cost=812.5):
    """DuckDB has no anomaly rows, so one flagged campaign is injected into the attention panel."""
    from lib import view

    ac, _, _ = dash_api
    # a campaign active in the last 14 days, so every window the tests use still offers it
    campaign = ac.filter_options("deep_dive", [("date_from", "2024-01-17"), ("date_to", "2024-01-30"),
                                              ("platform", platform)])["campaign_name"]["values"][0]
    real = view.panels

    def flagged(page, params):
        out = real(page, params)
        for p in out:
            if p["role"] == "attention":
                p["rows"] = [{"platform": platform, "campaign_name": campaign, "worst": "CRITICAL", "flagged_days": 2,
                              "last_flagged": "2024-01-29", "excess_cost": excess_cost}]
        return out

    monkeypatch.setattr(view, "panels", flagged)
    return campaign


def test_needs_attention_opens_the_campaign_in_the_deep_dive_with_the_same_dates(dash_api, monkeypatch):
    campaign = _flag(monkeypatch, dash_api)
    at = run("Home.py", ov_preset="Last 14 days")
    assert not at.exception, at.exception
    assert any(campaign in m.value and "CRITICAL" in m.value for m in at.markdown)
    at.button(key="open_0").click().run()
    assert not at.exception, at.exception
    assert at.session_state["dd_platform"] == "Google" and at.session_state["dd_campaign_name"] == [campaign]
    assert at.session_state["dd_preset"] == "Custom"
    assert tuple(at.session_state["dd_custom"]) == (date(2024, 1, 17), date(2024, 1, 30))
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


def _cards(monkeypatch, cards, checked=("cost per sale",), at_stake=0.0):
    from lib import view

    monkeypatch.setattr(view, "insights", lambda params: {"cards": cards, "checked": list(checked), "problems": [],
                                                          "writer": "templates", "at_stake": at_stake})


CARD = {"id": "mix:Google", "kind": "mix", "severity": "high", "stake": 1234.5, "title": "Rebalance spend away from Google",
        "headline": "Google took 60% of spend but brought 25% of sales for $1.2K.", "action": "Move a slice.",
        "why": "Its cost per sale is $60.", "confidence": "high (300 sales)",
        "numbers": [{"platform": "Google", "share of spend": "60%"}],
        "chart": {"spec": {"chart_type": "bar", "x": "platform", "y": "share", "color": "measure"},
                  "rows": [{"platform": "Google", "measure": "Share of spend", "share": 0.6}], "formats": {"share": "percent"}},
        "facts": "Google took 60% of spend but brought 25% of sales.",
        "loss": False, "stake_label": "to reallocate", "also": []}
LOSS = {**CARD, "id": "outlier:Google:c1", "kind": "outlier", "stake": 5000.0, "loss": True, "stake_label": "at stake",
        "title": "Cut or fix Google campaign c1", "also": ["It also paid $28.39 per sale, +38% from $20.60."]}


def test_insight_cards_render_ranked_with_charts(dash_api):
    at = run("pages/2_AI_Insights.py", ins_preset="Last 14 days")
    assert not at.exception, at.exception
    assert any("finding" in m.value or "Nothing needs attention" in m.value for m in at.markdown)


def test_card_text_shows_dollar_amounts_literally(dash_api, monkeypatch):
    """Review focus 4."""
    _cards(monkeypatch, [CARD])
    at = run("pages/2_AI_Insights.py")
    assert not at.exception, at.exception
    assert any("\\$1.2K" in m.value for m in at.markdown)
    assert any("HIGH" in m.value for m in at.markdown)  # a text badge, not colour alone


def test_ask_in_chat_goes_to_this_pages_chat(dash_api, monkeypatch):
    _cards(monkeypatch, [CARD])
    at = run("pages/2_AI_Insights.py")
    at.button(key="chat_mix:Google").click().run()
    assert not at.exception, at.exception
    asked = at.session_state["messages_insights"][0]
    assert asked["role"] == "user" and asked["content"] == "Why did this happen, and what should I check first? " + CARD["facts"]


def test_no_findings_says_so_and_lists_the_checks(dash_api, monkeypatch):
    _cards(monkeypatch, [], checked=("cost per sale", "channel mix"))
    at = run("pages/2_AI_Insights.py")
    text = " ".join(m.value for m in at.markdown)
    assert "Nothing needs attention in this period" in text and "channel mix" in text


def test_the_packs_questions_are_chat_suggestions(dash_api):
    at = run("pages/2_AI_Insights.py")
    assert any("best cost per acquisition" in b.label for b in at.sidebar.button)


FAILED_READ = {"cards": [], "checked": [], "problems": ["couldn't read the gold table"], "writer": "templates"}


@pytest.mark.parametrize("page", ["Home.py", "pages/2_AI_Insights.py"])
def test_a_failed_read_is_never_an_all_clear(dash_api, monkeypatch, page):
    """Final review I1."""
    from lib import view

    monkeypatch.setattr(view, "insights", lambda params: FAILED_READ)
    at = run(page)
    assert not at.exception, at.exception
    text = " ".join([m.value for m in at.markdown] + [c.value for c in at.caption])
    assert "Couldn't check this period." in text and "couldn't read the gold table" in text
    assert "Nothing needs attention" not in text


def test_what_changed_shows_problems_next_to_its_cards(dash_api, monkeypatch):
    from lib import view

    monkeypatch.setattr(view, "insights", lambda params: {"cards": [CARD], "checked": ["channel mix"],
                                                          "problems": ["couldn't read the anomalies table"],
                                                          "writer": "templates"})
    at = run("Home.py")
    assert not at.exception, at.exception
    assert any("couldn't read the anomalies table" in c.value for c in at.caption)


def test_a_failed_insights_call_on_the_overview_keeps_the_kpis(dash_api, monkeypatch):
    from lib import view
    from lib.api_client import ApiError

    def fails(params):
        raise ApiError("server", "The AdPilot service failed (HTTP 500).")

    monkeypatch.setattr(view, "insights", fails)
    at = run("Home.py")
    assert not at.exception, at.exception
    assert "The AdPilot service failed (HTTP 500)." in [e.value for e in at.error]
    assert at.metric and at.metric[0].value.startswith("$")


def test_a_negative_excess_cost_reads_as_cheaper(dash_api, monkeypatch):
    """Final review M5: a flagged day can cost less than usual; "about $-40 excess cost" is not a sentence."""
    _flag(monkeypatch, dash_api, excess_cost=-40.0)
    at = run("Home.py")
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "about \\$40 cheaper than usual" in text and "excess cost" not in text.split("Needs attention")[-1]


def test_the_comparison_fetch_asks_only_for_panels_a_page_overlays(monkeypatch):
    """Final review M3: no page draws a prior line on a `trend` panel, so none is fetched."""
    from lib import view

    asked: list[str] = []
    monkeypatch.setattr(view, "panels", lambda page, params: asked.extend(v for k, v in params if k == "panel") or [])
    m = {"panels": {"overview": [{"id": "k", "role": "kpi"}, {"id": "s", "role": "kpi_series"},
                                 {"id": "t", "role": "trend"}]}}
    view.prior_panels("overview", m, [], date(2024, 1, 8), date(2024, 1, 14))
    assert asked == ["k", "s"]


def _figures(at) -> dict:
    """The page's Plotly figures by their st.plotly_chart key."""
    return {c.proto.id.split("-", 2)[-1]: json.loads(c.proto.spec) for c in at.get("plotly_chart")}


def test_one_platform_draws_its_own_charts_in_its_colour(dash_api):
    """Live pass, finding 2."""
    from lib.theme import COLORS

    figs = _figures(run("pages/1_Channel_Deep_Dive.py", dd_platform="Google"))
    google = COLORS["forecast"]  # the pack: Google = forecast
    assert figs["fig_dd_trend"]["data"][0]["line"]["color"] == google
    assert figs["fig_google_quality_dist"]["data"][0]["marker"]["color"] == google
    assert figs["fig_google_impression_share"]["data"][0]["line"]["color"] == google
    assert figs["fig_leaderboard"]["data"][0]["marker"]["color"] == google
    everyone = _figures(run("pages/1_Channel_Deep_Dive.py", dd_platform="All"))
    assert everyone["fig_dd_trend"]["data"][0]["line"]["color"] == COLORS["brand"]  # "All" keeps the default


def test_the_overviews_comparisons_are_not_in_platform_colours(dash_api):
    """Live pass, finding 4: share of spend vs share of conversions drew in Facebook red and Google blue."""
    from lib.theme import OTHER

    figs = _figures(run("Home.py"))
    assert [t["marker"]["color"] for t in figs["fig_mix"]["data"]] == OTHER[:2]


def test_a_card_says_what_its_stake_is_and_only_losses_are_summed(dash_api, monkeypatch):
    """Live pass, finding 9."""
    _cards(monkeypatch, [LOSS, CARD], at_stake=5000.0)
    at = run("pages/2_AI_Insights.py")
    assert not at.exception, at.exception
    shown = [m.value for m in at.markdown]
    assert "**2 findings · about \\$5.0K at stake**" in shown  # not 5,000 + 1,234.5
    assert any(m.endswith("HIGH · about \\$5.0K at stake") for m in shown)
    assert any(m.endswith("HIGH · about \\$1.2K to reallocate") for m in shown)
    assert shown.count("Also: It also paid \\$28.39 per sale, +38% from \\$20.60.") == 1


def test_nothing_lost_means_no_at_stake_total(dash_api, monkeypatch):
    _cards(monkeypatch, [CARD], at_stake=0.0)
    at = run("pages/2_AI_Insights.py")
    assert "**1 finding**" in [m.value for m in at.markdown]
    assert not any("at stake" in m.value for m in at.markdown)


def test_what_changed_says_what_each_stake_is(dash_api, monkeypatch):
    _cards(monkeypatch, [LOSS, CARD], at_stake=5000.0)
    at = run("Home.py")
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "(about \\$5.0K at stake)" in text and "(about \\$1.2K to reallocate)" in text


WHY = {"text": "Ad price +20%, clicks per view -50%. Mostly fewer people clicked. It paid $5 more per sale.",
       "chart": {"spec": {"chart_type": "bar", "x": "measure", "y": "change"},
                 "rows": [{"measure": "Ad price", "change": 0.2}, {"measure": "Clicks per view", "change": -0.5}],
                 "formats": {"change": "percent"}}}


def test_a_card_with_a_why_explains_it_in_a_collapsed_expander(dash_api, monkeypatch):
    """Round 2: the engine's why replaces the model-driven "Investigate why"."""
    _cards(monkeypatch, [{**LOSS, "platform": "Google", "why_detail": WHY}, {**CARD, "why_detail": None}], at_stake=5000.0)
    at = run("pages/2_AI_Insights.py")
    assert not at.exception, at.exception
    whys = [e for e in at.expander if e.label == "Why this happened"]
    assert len(whys) == 1 and not whys[0].proto.expanded  # only the card that has one; closed until asked
    assert [m.value for m in whys[0].markdown] == ["Ad price +20%, clicks per view -50%. Mostly fewer people clicked. "
                                                   "It paid \\$5 more per sale."]
    assert "why_outlier:Google:c1" in _figures(at)
    assert "Investigate why" not in [b.label for b in at.button] and "Ask in chat" in [b.label for b in at.button]
    assert "investigations" not in at.session_state


def test_a_card_from_an_api_without_the_why_still_renders(dash_api, monkeypatch):
    _cards(monkeypatch, [CARD])  # no why_detail key at all: the dashboard can deploy before the API
    at = run("pages/2_AI_Insights.py")
    assert not at.exception, at.exception
    assert not [e for e in at.expander if e.label == "Why this happened"]
