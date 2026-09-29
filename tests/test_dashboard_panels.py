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
    assert all(set(p) == {"id", "title", "kind", "table", "platforms", "role"} for p in m["panels"]["overview"])
    assert m["insights"] and {f["column"] for f in m["filters"]} >= {"platform", "severity"}


def test_pacing_uses_the_briefs_projection(deps):
    from adpilot.dashboard.panels import pacing_rows

    out = pacing_rows(deps, TtlCache(0))
    rows = {r["platform"]: r for r in out["rows"]}
    assert out["as_of"] == "2024-01-30" and out["problems"] == []
    assert rows["Google"]["budget"] == 58000 and rows["Google"]["spent_mtd"] > 0
    assert rows["Google"]["basis"] == "last 7 days"  # the bundled forecast table is empty
    assert isinstance(rows["Google"]["off_pct"], float)


def test_a_bad_briefing_budget_is_a_pacing_problem_not_a_500(deps):
    import dataclasses
    from copy import deepcopy

    from adpilot.dashboard.panels import pacing_rows

    bad_raw = deepcopy(deps.pack.raw)
    bad_raw["briefing"]["budgets"]["Google"] = 0  # settings() rejects a non-positive budget with ValueError
    bad_deps = dataclasses.replace(deps, pack=dataclasses.replace(deps.pack, raw=bad_raw))

    out = pacing_rows(bad_deps, TtlCache(0))
    assert out["rows"] == [] and out["problems"]


def test_a_connector_error_does_not_leak_the_live_table_name(deps, cfg, monkeypatch):
    """Final-review finding 1: SqlSchema/SqlSyntax/DataSourceUnavailable messages come from the connector and can
    carry the live project:dataset.table; only our own guards' kinds (SqlPolicy, BudgetExceeded) are shown verbatim."""
    kpis = next(p for p in cfg.panels if p.id == "kpis")
    monkeypatch.setattr(panels, "execute",
                         lambda *a, **k: SqlError(kind="SqlSchema", message="Not found: proj:dataset.table"))
    r = run_panel(deps, cfg, kpis, FLT)
    assert "proj:dataset.table" not in r.error
    assert r.error == "SqlSchema: this panel could not be read"


def test_pacing_problems_do_not_leak_the_live_table_name(deps, monkeypatch):
    """Final-review finding 1: brief.load's problems are `"couldn't read the <name> table (<exc>)"`; the exception
    text (which can name the live table) must not reach viewers."""
    from adpilot.dashboard.panels import pacing_rows

    monkeypatch.setattr(panels, "load",
                         lambda *a, **k: ({}, ["couldn't read the flags table (NotFound: proj:dataset.table)"]))
    out = pacing_rows(deps, TtlCache(0))
    assert out["rows"] == [] and out["as_of"] is None
    assert out["problems"] == ["couldn't read the flags table"]


def test_filter_options_cache_key_ignores_range_values(deps, cfg, monkeypatch):
    """Final-review finding 2: option queries never read ranges, so two calls differing only in a range value must
    share a cache entry instead of each re-running every option query."""
    calls = []
    real = panels.execute
    monkeypatch.setattr(panels, "execute", lambda *a, **k: calls.append(1) or real(*a, **k))
    cache = TtlCache(60)
    a = Filters(date(2024, 1, 1), date(2024, 1, 30), ranges=(("spend", 0.0, 100.0),))
    b = Filters(date(2024, 1, 1), date(2024, 1, 30), ranges=(("spend", 50.0, 200.0),))
    filter_options(deps, cfg, "deep_dive", a, cache)
    first = len(calls)
    filter_options(deps, cfg, "deep_dive", b, cache)
    assert len(calls) == first


def test_a_failed_panel_does_not_stop_others_from_caching(deps, cfg, monkeypatch):
    """Final-review finding 3: page caching is per panel, so an always-failing panel is retried every load without
    evicting the panels next to it from the cache."""
    trend = next(p for p in cfg.panels if p.id == "spend_trend")
    bad = trend.model_copy(update={"chart": ChartSpec(chart_type="line", x="date", y="nope")})
    local_cfg = cfg.model_copy(update={"panels": [bad if p.id == "spend_trend" else p for p in cfg.panels]})
    calls = []
    real = panels.execute
    monkeypatch.setattr(panels, "execute", lambda *a, **k: calls.append(1) or real(*a, **k))
    cache = TtlCache(60)
    run_page(deps, local_cfg, "overview", FLT, cache, ids=("kpis", "spend_trend"))
    first = len(calls)
    results = run_page(deps, local_cfg, "overview", FLT, cache, ids=("kpis", "spend_trend"))
    assert len(calls) == first + 1  # only spend_trend (still failing) re-ran; kpis served from cache
    assert next(r for r in results if r.id == "spend_trend").error is not None


def test_a_format_on_a_missing_column_fails_only_that_panel(deps, cfg):
    kpis = next(p for p in cfg.panels if p.id == "kpis")
    r = run_panel(deps, cfg, kpis.model_copy(update={"formats": {"nope": "currency"}}), FLT)
    assert r.error.startswith("formats:") and r.rows == []


def test_results_carry_role_and_formats(deps, cfg):
    r = run_page(deps, cfg, "overview", FLT, TtlCache(0), ids=("kpis",))[0]
    assert r.role == "kpi" and r.formats["spend"] == "currency"


def test_meta_returns_roles_and_colours(deps, cfg):
    m = dashboard_meta(deps, cfg, TtlCache(0))
    assert m["colors"]["Google"] == "forecast"
    assert all("role" in p for page in m["panels"].values() for p in page)


def test_format_date_is_the_same_on_duckdb(deps):
    sql = deps.pack.render("SELECT FORMAT_DATE('%a', DATE '2024-01-01') AS d FROM {gold} LIMIT 1", "duckdb")
    assert execute(deps, sql).rows == [{"d": "Mon"}]
