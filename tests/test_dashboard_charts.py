"""Charts from API chart specs, in AdPilot colours."""

import pytest
from lib.charts import add_markers, add_prior, build_figure, pacing_bullets
from lib.formatters import fmt, label
from lib.theme import COLORS, SERIES, SEVERITY, platform_colors

ROWS = [{"platform": "Google", "spend": 1.0, "cpa": 2.0, "conversions": 3.0, "weekday": "Mon"},
        {"platform": "TikTok", "spend": 2.0, "cpa": 4.0, "conversions": 1.0, "weekday": "Tue"}]
EXTRA = {"bubble": {"size": "conversions"}, "heatmap": {"x": "weekday", "y": "platform", "z": "spend"}}
COLORS_BY_PLATFORM = platform_colors({"Facebook": "brand", "Google": "forecast", "TikTok": "audited"})


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
