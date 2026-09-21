import pytest

from adpilot.connectors import get_connector
from adpilot.core import schema
from adpilot.core.chart import ChartSpec, heuristic_chart, validate_spec
from adpilot.packs.loader import load_pack


def test_schema_summary_mixes_live_columns_and_pack_descriptions():
    pack = load_pack("ads")
    text = schema.summary(get_connector("duckdb", pack), pack)
    assert "fct_unified_marketing_performance  (gold)" in text
    assert "cpa DOUBLE — spend / conversions" in text
    assert "tbl_forecast  (forecast)" in text


def test_validate_spec():
    spec = ChartSpec(chart_type="bar", x="platform", y="spend", color="nope")
    out = validate_spec(spec, ["platform", "spend"])
    assert out.color is None
    with pytest.raises(ValueError, match="Available: platform, spend"):
        validate_spec(ChartSpec(chart_type="bar", x="zzz", y="spend"), ["platform", "spend"])


def test_heuristic_chart():
    assert heuristic_chart(["date", "platform", "spend"], ["spend"]).chart_type == "line"
    assert heuristic_chart(["platform", "spend"], ["spend"]).x == "platform"
    assert heuristic_chart(["campaign_name", "cpa"], ["cpa"]).x == "campaign_name"
    assert heuristic_chart(["a"], []) is None
