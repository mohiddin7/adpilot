"""The rules the dashboard pages depend on, tested without Streamlit."""

from datetime import date

import pytest
from lib.controls import (
    ASK_WHY,
    QUESTION_LIMIT,
    applies_to,
    clamp_pair,
    clamp_range,
    context_line,
    delta_pct,
    keep_valid,
    md,
    preset_range,
    prior_range,
    to_params,
    with_context,
    with_dates,
)

MIN, MAX = date(2024, 1, 1), date(2024, 1, 30)


def test_presets_count_back_from_the_newest_day_in_the_data():
    assert preset_range("Last 7 days", MIN, MAX) == (date(2024, 1, 24), MAX)
    assert preset_range("Month to date", MIN, MAX) == (MIN, MAX)
    assert preset_range("Last 90 days", MIN, MAX) == (MIN, MAX)  # never before the first day of data
    with pytest.raises(ValueError):
        preset_range("Custom", MIN, MAX)


def test_ranges_longer_than_the_api_allows_are_clamped():
    assert clamp_range(date(2023, 1, 1), date(2024, 6, 30)) == (date(2023, 7, 1), date(2024, 6, 30), True)
    assert clamp_range(MIN, MAX) == (MIN, MAX, False)


def test_prior_range_is_the_same_length_immediately_before():
    assert prior_range(date(2024, 1, 8), date(2024, 1, 14)) == (date(2024, 1, 1), date(2024, 1, 7))


def test_with_dates_keeps_every_filter():
    params = [("date_from", "2024-01-08"), ("date_to", "2024-01-14"), ("platform", "Google"), ("spend_min", "5.0")]
    assert with_dates(params, date(2024, 1, 1), date(2024, 1, 7)) == [
        ("date_from", "2024-01-01"), ("date_to", "2024-01-07"), ("platform", "Google"), ("spend_min", "5.0")]


def test_params_leave_out_ranges_still_at_their_bounds():
    params = to_params(MIN, MAX, {"platform": ["Google"], "campaign_name": []},
                       {"spend": (0.0, 500.0), "cpa": (5.0, 90.0)}, {"spend": (0.0, 500.0), "cpa": (1.0, 100.0)})
    assert params == [("date_from", "2024-01-01"), ("date_to", "2024-01-30"), ("platform", "Google"),
                      ("cpa_min", "5.0"), ("cpa_max", "90.0")]


def test_stale_choices_are_dropped_and_sliders_squeezed():
    """Review focus 2: st.multiselect and st.slider raise on values outside their options or bounds."""
    assert keep_valid(["a", "gone"], ["a", "b"]) == ["a"]
    assert clamp_pair((0.0, 900.0), 10.0, 500.0) == (10.0, 500.0)
    assert clamp_pair((600.0, 700.0), 10.0, 500.0) == (500.0, 500.0)


def test_platform_specific_controls_follow_the_same_rule_as_the_api():
    assert applies_to(None, []) and applies_to(["Google"], ["Google"])
    assert not applies_to(["Google"], []) and not applies_to(["Google"], ["Google", "TikTok"])


def test_delta_is_none_without_a_usable_prior():
    assert delta_pct(110, 100) == pytest.approx(10.0)
    assert delta_pct(5, 0) is None and delta_pct(None, 3) is None and delta_pct(3, None) is None


def test_context_line_is_short_and_says_what_is_selected():
    line = context_line("Overview", MIN, MAX, {"platform": ["Google"], "campaign_name": [f"c{i}" for i in range(9)]})
    assert line.startswith("Overview page; 2024-01-01 to 2024-01-30; platform: Google")
    assert "and 6 more" in line and len(line) <= 200


def test_context_is_dropped_rather_than_push_a_question_over_the_limit():
    """Review focus 5: over 600 characters, ask() refuses the question outright."""
    assert with_context("why?", "Overview page") == "[Context: Overview page] why?"
    long_question = "x" * 590
    assert with_context(long_question, "Overview page; 2024-01-01 to 2024-01-30") == long_question


def test_md_escapes_dollar_signs():
    """Review focus 4: Streamlit markdown renders $...$ as LaTeX."""
    assert md("spent $5K of $7K") == "spent \\$5K of \\$7K"


def test_ask_in_chat_leaves_room_for_a_cards_facts():
    """The card's facts (FACTS_MAX on the server) plus the question must fit the analyst's limit untrimmed."""
    from adpilot.dashboard.insights import FACTS_MAX

    assert ASK_WHY.endswith("? ") and len(ASK_WHY) + FACTS_MAX <= QUESTION_LIMIT
