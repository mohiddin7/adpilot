"""The rules the dashboard pages depend on, tested without Streamlit."""

from datetime import date

import pytest
from lib.controls import (
    ASK_WHY,
    QUESTION_LIMIT,
    answer_md,
    applies_to,
    caveat_text,
    clamp_pair,
    clamp_range,
    context_line,
    delta_pct,
    distinct_names,
    filter_summary,
    header_html,
    keep_valid,
    md,
    no_sales_line,
    preset_range,
    prior_range,
    to_params,
    with_context,
    with_dates,
)
from lib.formatters import format_for

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


def test_ask_in_chat_leaves_room_for_a_cards_facts():
    """The card's facts (FACTS_MAX on the server) plus the question must fit the analyst's limit untrimmed."""
    from adpilot.dashboard.insights import FACTS_MAX

    assert ASK_WHY.endswith("? ") and len(ASK_WHY) + FACTS_MAX <= QUESTION_LIMIT


def test_md_shows_data_as_typed():
    assert md("[click](http://x) #1 *Sale* `x`") == r"\[click\](http\://x) \#1 \*Sale\* \`x\`"
    assert md("spent $5K of $7K") == r"spent \$5K of \$7K"
    assert md(":red[big] &copy; <b>x</b> ~~y~~ a|b") == r"\:red\[big\] \&copy; \<b\>x\</b\> \~\~y\~\~ a\|b"
    assert md("- not a list") == r"\- not a list" and md("1. not a list") == r"1\. not a list"
    assert md("two\n# lines") == r"two \# lines"  # one line: a newline can't start a heading


@pytest.mark.parametrize("raw,expected", [
    ("Spend rose.\n---\nMore", "Spend rose.\n\n---\nMore"),          # setext H2
    ("Total\n===", "Total\n\n==="),                                  # setext H1
    ("# Title\nbody", "**Title**\nbody"),
    ("### Spend by **platform** ###", "**Spend by platform**"),
    ("**Spend rose 5%", "\\*\\*Spend rose 5%"),
    ("You spent $5K", "You spent \\$5K"),
    ("```sql\n# not a heading\nSELECT '$1', '**'\n```", "```sql\n# not a heading\nSELECT '$1', '**'\n```"),  # review focus 5
    ("| a | b |\n|---|---|\n| 1 | 2 |", "| a | b |\n|---|---|\n| 1 | 2 |"),
    ("- one\n- two", "- one\n- two"),
    ("See [the docs](https://x.y)", "See [the docs](https://x.y)"),
])
def test_answer_md_cannot_take_over_the_page(raw, expected):
    assert answer_md(raw) == expected


@pytest.mark.parametrize("caveat,expected", [
    ("Rerun: BudgetExceeded", "The analyst hit its query limit, so this answer may be incomplete."),
    ("Rerun: SqlSchema", "The first attempt failed, so the analyst answered on a second try."),
    ("OutputPolicy", "Part of the answer was withheld because it named internal details."),
    ("GuardDegraded", "One of the question checks was unavailable, so only the basic checks ran."),
    ("classifier:jev", "A safety check stopped this question."),
    ("InputPolicy", "A safety check stopped this question."),
    ("OutOfScope", "This question is outside the marketing data, so the analyst did not answer it."),
    ("ModelUnavailable: answered without the language model (timeout).",
     "The language model was unavailable, so this answer comes from a pre-defined query."),
    ("SomethingNew", "The analyst noted a limitation with this answer."),
    ("Spend for March is still loading.", "Spend for March is still loading."),
])
def test_caveats_read_as_sentences(caveat, expected):
    assert caveat_text(caveat) == expected


@pytest.mark.parametrize("column,kind", [
    ("spend", "currency"), ("cpa", "currency"), ("excess_cost", "currency"), ("conversion_value", "currency"),
    ("ctr", "percent"), ("click_rate", "percent"), ("search_impression_share", "percent"),
    ("roas", "multiple"), ("roas_google", "multiple"), ("conversions", "number"), ("current_spend_pct", "number"),
])
def test_chat_columns_take_the_dashboards_formats(column, kind):
    assert format_for(column) == kind


def test_campaigns_sharing_a_name_are_told_apart():
    rows = [{"platform": "Google", "campaign_id": "g1", "campaign_name": "Shared"},
            {"platform": "TikTok", "campaign_id": "t1", "campaign_name": "Shared"},
            {"platform": "Google", "campaign_id": "g2", "campaign_name": "Solo"},
            {"platform": "Google", "campaign_id": "g2", "campaign_name": "Solo"}]  # same campaign twice (two days)
    out = distinct_names(rows)
    assert [r["campaign_name"] for r in out] == ["Shared (Google)", "Shared (TikTok)", "Solo", "Solo"]
    assert out[0]["campaign_name_raw"] == "Shared" and "campaign_name_raw" not in out[2]
    same = distinct_names([{"platform": "Google", "campaign_id": i, "campaign_name": "X"} for i in ("g1", "g2")])
    assert [r["campaign_name"] for r in same] == ["X (Google g1)", "X (Google g2)"]
    assert distinct_names([{"campaign_name": "X"}, {"campaign_name": "X"}]) == [{"campaign_name": "X"}] * 2  # no ids


def test_campaigns_with_no_sales_are_named_under_the_map():
    rows = [{"campaign_name": n, "spend": s, "conversions": 0} for n, s in
            (("A", 500.0), ("B", 1500.0), ("C*", 50.0), ("D", 20.0))] + [{"campaign_name": "E", "spend": 9.0, "conversions": 3}]
    assert no_sales_line(rows) == md("4 campaigns spent $2.1K with no sales: B ($1.5K), A ($500), C* ($50) and 1 more.")
    assert no_sales_line(rows[-1:]) is None
    assert no_sales_line(rows[:1]) == md("1 campaign spent $500 with no sales: A ($500).")


def test_the_header_names_the_product_the_page_and_the_data_date():
    assert header_html("Overview", "2024-01-30") == (
        '<div class="ad-header"><span class="ad-brand">AdPilot</span><span class="ad-page">Overview</span>'
        '<span class="ad-asof">Data through 2024-01-30</span></div>')
    assert "ad-asof" not in header_html("Chat", None)
    assert "&lt;x&gt;" in header_html("<x>", None)


def test_the_filter_summary_counts_filters_not_values():
    params = [("date_from", "2024-01-01"), ("date_to", "2024-01-30"), ("campaign_name", "A"), ("campaign_name", "B"),
              ("spend_min", "10.0"), ("spend_max", "90.0")]
    assert filter_summary(date(2024, 1, 1), date(2024, 1, 30), params) == "2024-01-01 to 2024-01-30 · 2 filters set"
    assert filter_summary(date(2024, 1, 1), date(2024, 1, 30), params[:2]) == "2024-01-01 to 2024-01-30"
