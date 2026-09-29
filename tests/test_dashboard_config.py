"""The pack's dashboard section: parsed at boot, and a typo fails loudly there."""

import copy
import dataclasses

import pytest

from adpilot.dashboard.config import load_dashboard


def _with(pack, mutate):
    raw = copy.deepcopy(pack.raw)
    mutate(raw["dashboard"])
    return dataclasses.replace(pack, raw=raw)


def test_the_ads_pack_dashboard_loads(pack):
    cfg = load_dashboard(pack)
    assert {p.id for p in cfg.panels_for("overview")} >= {"kpis", "spend_trend", "needs_attention", "budget_plan"}
    assert {p.id for p in cfg.panels_for("deep_dive")} >= {"dd_kpis", "dd_trend", "dd_campaigns", "google_quality"}
    assert cfg.filter("platform").values == ["Facebook", "Google", "TikTok"]
    assert "sub_group_name" not in {f.column for f in cfg.filters_for("overview")}
    assert cfg.insights


def test_a_filter_on_a_column_the_table_lacks_fails(pack):
    def mutate(d):
        d["filters"].append(
            {"column": "no_such_column", "label": "x", "type": "categorical", "tables": ["gold"], "pages": ["overview"]}
        )

    with pytest.raises(ValueError, match="not a column"):
        load_dashboard(_with(pack, mutate))


def test_a_chart_on_a_kpi_panel_fails(pack):
    def mutate(d):
        d["panels"][0]["chart"] = {"chart_type": "bar", "x": "a", "y": "b"}  # panels[0] is `kpis`, kind kpi

    with pytest.raises(ValueError, match="kind: chart"):
        load_dashboard(_with(pack, mutate))


def test_an_unknown_placeholder_fails(pack):
    def mutate(d):
        d["panels"][0]["sql"] = "SELECT 1 AS x FROM {gold} WHERE {whre}"

    with pytest.raises(ValueError, match="placeholder"):
        load_dashboard(_with(pack, mutate))


def test_where_on_a_table_without_a_date_column_fails(pack):
    def mutate(d):
        d["panels"].append({"id": "x", "title": "x", "page": "overview", "kind": "table", "role": "details",
                            "table": "budget", "sql": "SELECT platform FROM {budget} WHERE {where}"})

    with pytest.raises(ValueError, match="no 'date' column"):
        load_dashboard(_with(pack, mutate))


def test_values_on_a_range_filter_fail(pack):
    def mutate(d):
        next(f for f in d["filters"] if f["column"] == "spend")["values"] = ["1"]

    with pytest.raises(ValueError, match="only a categorical"):
        load_dashboard(_with(pack, mutate))


def test_a_misspelt_key_fails_instead_of_being_ignored(pack):
    def mutate(d):
        d["panels"][0]["titel"] = "x"

    with pytest.raises(ValueError):
        load_dashboard(_with(pack, mutate))


def test_an_unknown_platform_on_a_panel_fails(pack):
    def mutate(d):
        next(p for p in d["panels"] if p["id"] == "google_quality")["platforms"] = ["Gogle"]

    with pytest.raises(ValueError, match="platform"):
        load_dashboard(_with(pack, mutate))


def test_every_panel_needs_a_known_role(pack):
    def missing(d):
        d["panels"][0].pop("role")

    def unknown(d):
        d["panels"][0]["role"] = "hero"

    for mutate in (missing, unknown):
        with pytest.raises(ValueError, match="role"):
            load_dashboard(_with(pack, mutate))


def test_an_unknown_format_fails(pack):
    def mutate(d):
        d["panels"][0]["formats"] = {"spend": "euros"}

    with pytest.raises(ValueError, match="formats"):
        load_dashboard(_with(pack, mutate))


@pytest.mark.parametrize("colors,match", [({"Facebook": "critical"}, "colors"), ({"Gogle": "brand"}, "Gogle")])
def test_platform_colours_must_be_series_colours_for_known_platforms(pack, colors, match):
    def mutate(d):
        d["colors"] = colors

    with pytest.raises(ValueError, match=match):
        load_dashboard(_with(pack, mutate))


def test_the_ads_pack_pins_one_colour_per_platform(pack):
    assert load_dashboard(pack).colors == {"Facebook": "brand", "Google": "forecast", "TikTok": "audited"}
