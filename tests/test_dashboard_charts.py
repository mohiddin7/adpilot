"""Charts from API chart specs, in AdPilot colours."""

from pathlib import Path

import pytest
import yaml
from lib.charts import add_markers, add_prior, build_figure, pacing_bullets
from lib.formatters import fmt, label
from lib.theme import (
    COLORS,
    MUTED,
    OTHER,
    PALETTE,
    SERIES,
    SEVERITY,
    SYMBOLS,
    platform_colors,
    platform_colour,
)

ROWS = [{"platform": "Google", "spend": 1.0, "cpa": 2.0, "conversions": 3.0, "weekday": "Mon"},
        {"platform": "TikTok", "spend": 2.0, "cpa": 4.0, "conversions": 1.0, "weekday": "Tue"}]
EXTRA = {"bubble": {"size": "conversions"}, "heatmap": {"x": "weekday", "y": "platform", "z": "spend"}}
COLORS_BY_PLATFORM = platform_colors({"Facebook": "brand", "Google": "forecast", "TikTok": "audited"})
PACK = yaml.safe_load((Path(__file__).resolve().parents[1] / "packs" / "ads" / "pack.yaml").read_text())
PLATFORM_HEX = set(platform_colors(PACK["dashboard"]["colors"]).values())  # what the pack really assigns
PALETTE_HUE = {spec[tier]: spec["hue"] for spec in PALETTE["colors"].values() for tier in ("light", "dark")}


@pytest.mark.parametrize("kind", ["bar", "line", "scatter", "pie", "area", "bubble", "funnel", "heatmap", "bar_h"])
def test_every_chart_type_builds(kind):
    spec = {"chart_type": kind, "x": "platform", "y": "spend", "color": None, **EXTRA.get(kind, {})}
    fig = build_figure(spec, ROWS)
    assert fig is not None and len(fig.data) >= 1


def test_nothing_to_draw_is_none():
    assert build_figure({"chart_type": "bar", "x": "a", "y": "b"}, []) is None
    assert build_figure({"chart_type": "bar", "x": "a", "y": "missing"}, [{"a": 1, "b": 2}]) is None


def test_an_unknown_colour_column_is_ignored():
    assert build_figure({"chart_type": "line", "x": "platform", "y": "spend", "color": "nope"}, ROWS) is not None


def test_series_use_the_brand_colours_never_the_alarm_colours():
    assert SERIES[0] == COLORS["brand"] == "#a8515c"
    assert COLORS["critical"] not in SERIES and COLORS["open"] not in SERIES


def _trace_colour(fig, name):
    t = next(t for t in fig.data if t.name == name or (getattr(t, "legendgroup", None) == name))
    return t.marker.color if t.type != "scatter" or t.mode == "markers" else t.line.color


def test_a_platform_keeps_its_colour_across_charts():
    line = build_figure({"chart_type": "line", "x": "weekday", "y": "cpa", "color": "platform"}, ROWS, None, COLORS_BY_PLATFORM)
    bubble = build_figure({"chart_type": "bubble", "x": "spend", "y": "cpa", "size": "conversions", "color": "platform"},
                          ROWS, None, COLORS_BY_PLATFORM)
    assert _trace_colour(line, "Google") == _trace_colour(bubble, "Google") == COLORS["forecast"]
    assert _trace_colour(line, "TikTok") == COLORS["audited"]


def test_axes_and_hovers_follow_the_formats():
    fig = build_figure({"chart_type": "line", "x": "weekday", "y": "cpa"}, ROWS, {"cpa": "currency"})
    assert fig.layout.yaxis.tickformat.startswith("$") and fig.layout.yaxis.hoverformat.startswith("$")


def test_the_mean_reference_is_weighted_by_size():
    fig = build_figure({"chart_type": "bubble", "x": "spend", "y": "cpa", "size": "conversions", "reference": "mean_y"},
                       ROWS, {"cpa": "currency"})
    line = next(s for s in fig.layout.shapes if s.y0 == s.y1)
    assert line.y0 == pytest.approx((2 * 3 + 4 * 1) / 4)
    assert {a.text for a in fig.layout.annotations} >= {"scale", "watch", "fix", "cut"}


def test_prior_and_markers_overlay():
    fig = build_figure({"chart_type": "line", "x": "date", "y": "spend"}, [{"date": "2024-01-02", "spend": 5.0}])
    add_prior(fig, [{"date": "2024-01-01", "spend": 4.0}], "date", "spend", 1)
    add_markers(fig, [{"date": "2024-01-02", "severity": "CRITICAL", "campaign_name": "c", "platform": "Google",
                       "observed_cpa": 9.0, "usual_cpa": 3.0}], [{"date": "2024-01-02", "spend": 5.0}], "date", "spend")
    dashed = next(t for t in fig.data if t.name == "Previous period")
    assert dashed.line.dash == "dash" and str(dashed.x[0])[:10] == "2024-01-02"
    crit = next(t for t in fig.data if t.name == "Critical")
    assert crit.marker.color == SEVERITY["CRITICAL"] and list(crit.y) == [5.0]


def test_pacing_bullets_skip_platforms_without_a_budget():
    rows = [{"platform": "Google", "budget": 100.0, "spent_mtd": 40.0, "projected": 110.0},
            {"platform": "TikTok", "budget": None, "spent_mtd": 5.0, "projected": None}]
    fig = pacing_bullets(rows)
    assert fig is not None and all(list(t.y) == ["Google"] for t in fig.data)
    assert pacing_bullets([]) is None


@pytest.mark.parametrize("value,kind,shown", [(1234.5, "currency", "$1.2K"), (0.01234, "percent", "1.23%"),
                                              (1.789, "multiple", "1.79x"), (13363, "number", "13.4K"),
                                              (None, "currency", "—"), (5, None, "5")])
def test_fmt(value, kind, shown):
    assert fmt(value, kind) == shown


def test_labels_read_like_a_media_buyer_wrote_them():
    assert label("cpa") == "Cost per acquisition" and label("roas_google") == "ROAS (Google)"
    assert label("some_new_col") == "Some new col"


def test_a_heatmap_hover_and_colour_bar_follow_the_z_format():
    """Final review M2."""
    fig = build_figure({"chart_type": "heatmap", "x": "weekday", "y": "platform", "z": "spend"}, ROWS,
                       {"spend": "currency"})
    assert "$" in fig.data[0].hovertemplate and fig.data[0].colorbar.tickformat.startswith("$")


def test_a_single_platform_view_draws_single_series_in_that_platforms_colour():
    """Live pass, finding 2: brand red is Facebook's colour, so Google's own charts must not be red."""
    google = COLORS_BY_PLATFORM["Google"]
    line = build_figure({"chart_type": "line", "x": "weekday", "y": "cpa"}, ROWS, None, COLORS_BY_PLATFORM, single=google)
    bar = build_figure({"chart_type": "bar", "x": "weekday", "y": "spend"}, ROWS, None, COLORS_BY_PLATFORM, single=google)
    funnel = build_figure({"chart_type": "funnel", "x": "weekday", "y": "spend"}, ROWS, None, COLORS_BY_PLATFORM, single=google)
    assert line.data[0].line.color == bar.data[0].marker.color == funnel.data[0].marker.color == google
    default = build_figure({"chart_type": "line", "x": "weekday", "y": "cpa"}, ROWS, None, COLORS_BY_PLATFORM)
    assert default.data[0].line.color == SERIES[0]  # "All": the default stays


def test_severity_is_a_warm_ordered_scale_and_never_a_platform_colour():
    """Live pass, finding 3: Severe was blue, which is Google."""
    scale = [SEVERITY[s] for s in ("MODERATE", "SEVERE", "CRITICAL")]
    assert len(set(scale)) == 3 and set(scale) <= set(PALETTE_HUE) and not set(scale) & PLATFORM_HEX
    assert scale[-1] == COLORS["critical"]
    hues = [PALETTE_HUE[c] for c in scale]
    assert hues == sorted(hues, reverse=True) and max(hues) <= 90  # yellow toward red, nothing cool
    assert len({SYMBOLS[s] for s in SEVERITY}) == 3  # colour is never the only signal


@pytest.mark.parametrize("colors", [COLORS_BY_PLATFORM, None])
@pytest.mark.parametrize("column,values", [("plan", ["current", "recommended"]), ("week", ["last week", "this week"]),
                                           ("measure", ["Share of spend", "Share of sales"]),
                                           ("series", ["spent", "budget pace"])])
def test_series_that_are_not_platforms_never_wear_a_platform_colour(column, values, colors):
    """Live pass, finding 4: red and blue read as Facebook and Google."""
    rows = [{"platform": p, column: v, "spend": 1.0} for p in ("Facebook", "Google") for v in values]
    fig = build_figure({"chart_type": "bar", "x": "platform", "y": "spend", "color": column}, rows, None, colors)
    used = [t.marker.color for t in fig.data]
    assert set(used) == set(OTHER[:2])  # which name gets which is by sorted name, see the stable-colour test
    assert len(set(OTHER)) == len(OTHER) and set(OTHER) <= set(PALETTE_HUE) and not set(OTHER) & PLATFORM_HEX
    assert not {SEVERITY["SEVERE"], SEVERITY["CRITICAL"]} & set(OTHER)  # and never an alarm red


def test_funnel_numbers_use_the_apps_format_and_keep_the_step_rates():
    """Live pass, finding 6: the funnel said 31.05583M, 584.544k, 13.014k."""
    rows = [{"stage": "1. Impressions", "value": 31055830}, {"stage": "2. Clicks", "value": 584544},
            {"stage": "3. Conversions", "value": 13014}]
    trace = build_figure({"chart_type": "funnel", "x": "stage", "y": "value"}, rows, {"value": "number"}).data[0]
    assert list(trace.text) == ["31.1M", "584.5K", "13.0K"]
    assert trace.textinfo == "text+percent previous"


@pytest.mark.parametrize("x", ["name", "period", "series", "measure", "week", "plan", "date"])
def test_a_category_holder_gets_no_x_axis_title(x):
    """Live pass, finding 7: "Name" and "Period" under the bars say nothing."""
    fig = build_figure({"chart_type": "bar", "x": x, "y": "cpa"}, [{x: "a", "cpa": 1.0}, {x: "b", "cpa": 2.0}], {"cpa": "currency"})
    assert not fig.layout.xaxis.title.text and fig.layout.yaxis.title.text == "Cost per acquisition"


def test_every_evidence_chart_axis_reads_as_a_metric():
    assert [label(c) for c in ("cpa", "spend", "share", "platform")] == ["Cost per acquisition", "Spend", "Share", "Platform"]
    fig = build_figure({"chart_type": "bar", "x": "platform", "y": "share", "color": "measure"},
                       [{"platform": "Google", "measure": "Share of spend", "share": 0.6}], {"share": "percent"})
    assert fig.layout.xaxis.title.text == "Platform" and fig.layout.yaxis.title.text == "Share"


def test_the_legend_sits_above_the_plot_clear_of_the_x_axis_title():
    """Live pass, finding 5: the legend sat on the "Platform" axis title. Pixels are checked by screenshot."""
    fig = build_figure({"chart_type": "bar", "x": "platform", "y": "spend", "color": "weekday"}, ROWS)
    assert fig.layout.legend.y >= 1 and fig.layout.legend.yanchor == "bottom"


def test_a_series_keeps_its_colour_whatever_order_the_rows_come_in():
    """Review R4: "Share of spend" was green on the Overview and ochre on the insights mix card."""
    def chart(measures):
        rows = [{"platform": p, "measure": m, "share": 0.5} for p in ("Facebook", "Google") for m in measures]
        return build_figure({"chart_type": "bar", "x": "platform", "y": "share", "color": "measure"}, rows, None, COLORS_BY_PLATFORM)

    overview = chart(["Share of conversions", "Share of spend"])
    insights = chart(["Share of spend", "Share of sales"])
    assert _trace_colour(overview, "Share of spend") == _trace_colour(insights, "Share of spend") == OTHER[1]
    assert _trace_colour(overview, "Share of conversions") == _trace_colour(insights, "Share of sales") == OTHER[0]


def test_each_pacing_bar_is_its_platforms_colour():
    """Review R3: every platform's "spent so far" bar was brand red, which is Facebook."""
    rows = [{"platform": "Google", "budget": 100.0, "spent_mtd": 40.0, "projected": 110.0},
            {"platform": "TikTok", "budget": 90.0, "spent_mtd": 30.0, "projected": 80.0},
            {"platform": "Snap", "budget": 50.0, "spent_mtd": 10.0, "projected": 40.0}]
    spent = next(t for t in pacing_bullets(rows, COLORS_BY_PLATFORM).data if t.name == "Spent so far")
    # Snap has no colour: a neutral, never the brand (Facebook's colour; review round 3)
    assert list(spent.marker.color) == [COLORS["forecast"], COLORS["audited"], MUTED]
    assert next(t for t in pacing_bullets(rows).data if t.name == "Spent so far").marker.color == COLORS["brand"]


def test_a_platform_without_a_colour_is_neutral_not_the_brand():
    """Review round 3: the brand is Facebook's colour, so an unassigned platform must not borrow it."""
    assert platform_colour(COLORS_BY_PLATFORM, "Google") == COLORS["forecast"]
    assert platform_colour(COLORS_BY_PLATFORM, "Snap") == MUTED and MUTED not in PLATFORM_HEX | {COLORS["brand"]}
    assert platform_colour(COLORS_BY_PLATFORM, None) is None and platform_colour({}, "Google") is None  # the defaults


@pytest.mark.parametrize("kind", ["bar", "bar_h"])
def test_one_series_whose_categories_are_platforms_wears_each_platforms_colour(kind):
    """Review round 3: "Total spend by platform" drew every platform in Facebook red."""
    rows = [{"platform": "TikTok", "spend": 3.0}, {"platform": "Google", "spend": 5.0}, {"platform": "Facebook", "spend": 1.0}]
    fig = build_figure({"chart_type": kind, "x": "platform", "y": "spend"}, rows, None, COLORS_BY_PLATFORM)
    assert {t.name: t.marker.color for t in fig.data} == {"TikTok": COLORS["audited"], "Google": COLORS["forecast"],
                                                          "Facebook": COLORS["brand"]}
    assert fig.layout.barmode != "group"  # one bar per category: full width, not offset as if grouped
    other = build_figure({"chart_type": kind, "x": "platform", "y": "spend"}, [{"platform": "Snap", "spend": 1.0}], None,
                         COLORS_BY_PLATFORM)
    assert other.data[0].marker.color == SERIES[0]  # not every category is a platform: the default series


def test_a_comparison_with_the_account_names_its_axis():
    assert label("vs_account") == "Against the account"
