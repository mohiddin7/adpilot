"""Charts from API chart specs, in AdPilot colours."""

import pytest
from lib.charts import build_figure
from lib.theme import COLORS, SERIES

ROWS = [{"platform": "Google", "spend": 1.0}, {"platform": "TikTok", "spend": 2.0}]


@pytest.mark.parametrize("kind", ["bar", "line", "scatter", "pie", "area"])
def test_every_chart_type_builds(kind):
    fig = build_figure({"chart_type": kind, "x": "platform", "y": "spend", "color": None}, ROWS)
    assert fig is not None and len(fig.data) >= 1


def test_nothing_to_draw_is_none():
    assert build_figure({"chart_type": "bar", "x": "a", "y": "b"}, []) is None
    assert build_figure({"chart_type": "bar", "x": "a", "y": "missing"}, [{"a": 1, "b": 2}]) is None


def test_an_unknown_colour_column_is_ignored():
    assert build_figure({"chart_type": "line", "x": "platform", "y": "spend", "color": "nope"}, ROWS) is not None


def test_series_use_the_brand_colours_never_the_alarm_colours():
    assert SERIES[0] == COLORS["brand"] == "#a8515c"
    assert COLORS["critical"] not in SERIES and COLORS["open"] not in SERIES
