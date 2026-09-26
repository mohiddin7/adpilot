"""Panels run through execute() on DuckDB: every ads panel runs, failures stay per panel, results are cached."""

from datetime import date

import pytest

from adpilot.core.chart import ChartSpec
from adpilot.core.runtime import fresh_deps
from adpilot.core.tools import SqlError, execute
from adpilot.dashboard import panels
from adpilot.dashboard.config import load_dashboard
from adpilot.dashboard.filters import Filters
from adpilot.dashboard.panels import (
    TtlCache,
    dashboard_meta,
    filter_options,
    panel_applies,
    run_page,
    run_panel,
)

FLT = Filters(date(2024, 1, 1), date(2024, 1, 30))


@pytest.fixture(scope="module")
def cfg(pack):
    return load_dashboard(pack)


def _flt(*platforms, **categorical):
    cats = tuple(sorted({**({"platform": platforms} if platforms else {}), **categorical}.items()))
    return Filters(date(2024, 1, 1), date(2024, 1, 30), cats)


def test_execute_honours_a_higher_row_cap_for_trusted_queries(deps):
    sql = deps.pack.render("SELECT * FROM {gold}", "duckdb")
    assert len(execute(deps, sql).rows) == deps.pack.max_result_rows  # 100: the model's cap is unchanged
    assert len(execute(fresh_deps(deps), sql, max_rows=1000).rows) == 330


def test_render_fills_extra_placeholders(pack):
    assert pack.render("SELECT 1 FROM {gold} WHERE {where}", "duckdb", where="1 = 1").endswith("WHERE 1 = 1")


@pytest.mark.parametrize("page", ["overview", "deep_dive"])
@pytest.mark.parametrize("platform", [(), ("Facebook",), ("Google",), ("TikTok",)])
def test_every_ads_panel_runs_cleanly_on_duckdb(deps, cfg, page, platform):
    """CI's check that each panel's SQL runs and its chart names columns the SQL returns."""
    results = run_page(deps, cfg, page, _flt(*platform), TtlCache(0))
    assert results
    assert [(r.id, r.error) for r in results if r.error] == []


def test_platform_specific_panels_follow_the_platform_selection(cfg):
    google = next(p for p in cfg.panels if p.id == "google_quality")
    assert panel_applies(google, _flt("Google"))
    assert not panel_applies(google, _flt())
    assert not panel_applies(google, _flt("Google", "TikTok"))


def test_a_chart_naming_a_missing_column_fails_only_that_panel(deps, cfg):
    trend = next(p for p in cfg.panels if p.id == "spend_trend")
    bad = trend.model_copy(update={"chart": ChartSpec(chart_type="line", x="date", y="nope")})
    r = run_panel(deps, cfg, bad, FLT)
    assert r.error.startswith("chart:") and r.rows == []


def test_an_empty_pipeline_table_is_a_note_not_an_error(deps, cfg):
    budget = next(p for p in cfg.panels if p.id == "budget_plan")
    r = run_panel(deps, cfg, budget, FLT)
    assert r.error is None and r.rows == [] and r.note


def test_ids_limit_the_panels_run(deps, cfg):
    assert [r.id for r in run_page(deps, cfg, "overview", FLT, TtlCache(0), ids=("kpis",))] == ["kpis"]


def test_repeat_reads_come_from_the_cache(deps, cfg, monkeypatch):
    calls = []
    real = panels.execute
    monkeypatch.setattr(panels, "execute", lambda *a, **k: calls.append(1) or real(*a, **k))
    cache = TtlCache(60)
    run_page(deps, cfg, "overview", FLT, cache, ids=("kpis",))
    run_page(deps, cfg, "overview", FLT, cache, ids=("kpis",))
    assert len(calls) == 1


def test_a_failed_panel_is_not_cached(deps, cfg, monkeypatch):
    calls = []

    def down(*a, **k):
        calls.append(1)
        return SqlError(kind="DataSourceUnavailable", message="down")

    monkeypatch.setattr(panels, "execute", down)
    cache = TtlCache(60)
    run_page(deps, cfg, "overview", FLT, cache, ids=("kpis",))
    run_page(deps, cfg, "overview", FLT, cache, ids=("kpis",))
    assert len(calls) == 2


def test_ttl_cache_expires_and_caps():
    now = [0.0]
    c = TtlCache(10, max_entries=2, clock=lambda: now[0])
    c.put("a", 1)
    assert c.get("a") == 1
    now[0] = 11
    assert c.get("a") is None
    c.put("b", 2)  # "a" is expired but still stored: the dict is full now
    c.put("c", 3)  # full → cleared, then "c" stored
    assert c.get("b") is None and c.get("c") == 3


def test_filter_options_cascade(deps, cfg):
    everything = filter_options(deps, cfg, "deep_dive", FLT, TtlCache(0))
    facebook = filter_options(deps, cfg, "deep_dive", _flt("Facebook", campaign_name=("x",)), TtlCache(0))
    assert everything["platform"]["values"] == ["Facebook", "Google", "TikTok"]
    assert facebook["campaign_name"]["values"]  # choosing campaign "x" does not empty the campaign list...
    assert set(facebook["campaign_name"]["values"]) < set(everything["campaign_name"]["values"])
    assert facebook["sub_group_name"]["values"] == []  # ...but it does narrow the ad sets under it
    assert everything["spend"]["min"] <= everything["spend"]["max"]


def test_meta_reports_the_data_window_and_hides_sql(deps, cfg):
    m = dashboard_meta(deps, cfg, TtlCache(0))
    assert (m["date_min"], m["date_max"]) == ("2024-01-01", "2024-01-30")
    assert all(set(p) == {"id", "title", "kind", "table", "platforms"} for p in m["panels"]["overview"])
    assert m["insights"] and {f["column"] for f in m["filters"]} >= {"platform", "severity"}
