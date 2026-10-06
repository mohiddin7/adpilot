"""Panels that show the same quantity must agree (spec 5.3a): KPI totals, the trend's sum, the campaign tables, the
funnel, the mix shares and the flows, over the same window and platform."""

from collections import defaultdict
from datetime import date

import pytest

from adpilot.dashboard.config import load_dashboard
from adpilot.dashboard.filters import Filters
from adpilot.dashboard.panels import TtlCache, run_page

PLATFORMS = [(), ("Facebook",), ("Google",), ("TikTok",)]


@pytest.fixture(scope="module")
def cfg(pack):
    return load_dashboard(pack)


def _page(deps, cfg, page, platforms):
    flt = Filters(date(2024, 1, 1), date(2024, 1, 30), (("platform", platforms),) if platforms else ())
    out = {r.id: r for r in run_page(deps, cfg, page, flt, TtlCache(0))}
    assert [(i, r.error) for i, r in out.items() if r.error] == []
    return out


def _sum(panel, col):
    return sum(row[col] or 0 for row in panel.rows)


def _near(total, expected, panel):  # each row is rounded to the cent
    assert total == pytest.approx(expected, abs=0.01 * max(len(panel.rows), 1))


@pytest.mark.parametrize("platforms", PLATFORMS)
def test_overview_panels_agree(eval_deps, cfg, platforms):
    p = _page(eval_deps, cfg, "overview", platforms)
    kpi = p["kpis"].rows[0]
    _near(_sum(p["kpi_daily"], "spend"), kpi["spend"], p["kpi_daily"])
    assert _sum(p["kpi_daily"], "conversions") == pytest.approx(kpi["conversions"])
    _near(_sum(p["efficiency"], "spend"), kpi["spend"], p["efficiency"])  # the fixture has no zero-sales campaign: see test_efficiency_keeps_zero_sales_campaigns
    assert _sum(p["efficiency"], "conversions") == pytest.approx(kpi["conversions"])
    funnel = {r["stage"]: r["value"] for r in p["funnel"].rows}
    assert funnel["3. Conversions"] == pytest.approx(kpi["conversions"])
    assert funnel["1. Impressions"] >= funnel["2. Clicks"] >= funnel["3. Conversions"]
    for measure in ("Share of spend", "Share of conversions"):
        assert sum(r["share"] for r in p["mix"].rows if r["measure"] == measure) == pytest.approx(1, abs=0.002)
    by_platform = defaultdict(float)
    for r in p["efficiency"].rows:
        by_platform[r["platform"]] += r["spend"]
    for r in p["mix"].rows:
        if r["measure"] == "Share of spend":  # shares are rounded to 4 decimals
            assert r["share"] == pytest.approx(by_platform[r["platform"]] / kpi["spend"], abs=0.0001)


def test_budget_panels_agree(eval_deps, cfg):
    p = _page(eval_deps, cfg, "overview", ())
    rec = {r["platform"]: r["spend"] for r in p["budget_plan"].rows if r["plan"] == "recommended"}
    into = defaultdict(float)
    for r in p["budget_flow"].rows:
        into[r["target"].removeprefix("Recommended: ")] += r["spend"]
    assert dict(into) == pytest.approx({k: v for k, v in rec.items() if v > 0}, abs=0.03)


@pytest.mark.parametrize("platforms", PLATFORMS)
def test_deep_dive_panels_agree(eval_deps, cfg, platforms):
    d = _page(eval_deps, cfg, "deep_dive", platforms)
    kpi = d["dd_kpis"].rows[0]
    for pid in ("dd_campaigns", "dd_subgroups", "dd_weekday", "money_flow"):
        _near(_sum(d[pid], "spend"), kpi["spend"], d[pid])
    funnel = {r["stage"]: r["value"] for r in d["dd_funnel"].rows}
    assert funnel["1. Impressions"] == pytest.approx(_sum(d["dd_campaigns"], "impressions"))
    assert funnel["2. Clicks"] == pytest.approx(_sum(d["dd_campaigns"], "clicks"))
    assert funnel["3. Conversions"] == pytest.approx(kpi["conversions"])
    _near(_sum(d["dd_daily"], "spend"), kpi["spend"], d["dd_daily"])
    assert _sum(d["dd_daily"], "conversions") == pytest.approx(kpi["conversions"])
    ov = _page(eval_deps, cfg, "overview", platforms)
    assert d["dd_kpis"].rows == ov["kpis"].rows  # the same SQL on the same filters
    assert d["dd_attention"].rows == ov["attention"].rows  # same SQL, same last-7-days anchor


def test_efficiency_keeps_zero_sales_campaigns(pack, cfg):
    from adpilot.connectors import get_connector
    from adpilot.core import schema
    from adpilot.core.audit import MemorySink
    from adpilot.core.tools import AgentDeps

    con = get_connector("duckdb", pack)
    con.execute_script("UPDATE fct_unified_marketing_performance SET conversions = 0 WHERE campaign_id = "
                       "(SELECT MIN(campaign_id) FROM fct_unified_marketing_performance)")
    deps = AgentDeps(connector=con, pack=pack, schema_text=schema.summary(con, pack), audit=MemorySink())
    p = _page(deps, cfg, "overview", ())
    _near(_sum(p["efficiency"], "spend"), p["kpis"].rows[0]["spend"], p["efficiency"])
    assert any(r["conversions"] == 0 for r in p["efficiency"].rows)
