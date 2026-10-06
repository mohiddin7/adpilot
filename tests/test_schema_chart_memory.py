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


def test_the_models_chart_types_are_unchanged():
    assert ChartSpec.model_json_schema()["properties"]["chart_type"]["enum"] == ["bar", "line", "scatter", "pie", "area"]


def test_panel_chart_types_need_their_extra_columns():
    from adpilot.core.chart import PanelChartSpec
    with pytest.raises(ValueError, match="size"):
        PanelChartSpec(chart_type="bubble", x="spend", y="cpa")
    with pytest.raises(ValueError, match="z"):
        PanelChartSpec(chart_type="heatmap", x="weekday", y="platform")
    assert PanelChartSpec(chart_type="funnel", x="stage", y="value").reference is None


def test_a_sankey_needs_a_target_that_exists():
    from adpilot.core.chart import PanelChartSpec, validate_spec

    with pytest.raises(ValueError, match="target"):
        PanelChartSpec(chart_type="sankey", x="source", y="spend")
    spec = PanelChartSpec(chart_type="sankey", x="source", y="spend", target="target")
    with pytest.raises(ValueError, match="target"):
        validate_spec(spec, ["source", "spend"])
    assert validate_spec(spec, ["source", "target", "spend"]) == spec


def test_validate_spec_checks_size_and_z_and_keeps_the_class():
    from adpilot.core.chart import PanelChartSpec
    spec = PanelChartSpec(chart_type="bubble", x="spend", y="cpa", size="conversions", reference="mean_y")
    assert isinstance(validate_spec(spec, ["spend", "cpa", "conversions"]), PanelChartSpec)
    with pytest.raises(ValueError, match="conversions"):
        validate_spec(spec, ["spend", "cpa"])
    heat = PanelChartSpec(chart_type="heatmap", x="weekday", y="platform", z="spend")
    with pytest.raises(ValueError, match="spend"):
        validate_spec(heat, ["weekday", "platform"])
