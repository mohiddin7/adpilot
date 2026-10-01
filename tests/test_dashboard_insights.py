"""/insights: the brief's analyses over the viewer's window, ranked by stake, with evidence charts (DuckDB, no model)."""

import copy
import dataclasses
import json
from datetime import date

import pytest

from adpilot.core.chart import PanelChartSpec, validate_spec
from adpilot.core.tools import SqlError
from adpilot.dashboard import insights
from adpilot.dashboard.config import load_dashboard
from adpilot.dashboard.filters import Filters
from adpilot.dashboard.panels import TtlCache

WINDOW = Filters(date(2024, 1, 16), date(2024, 1, 30))


@pytest.fixture
def cfg(pack):
    return load_dashboard(pack)


@pytest.fixture
def eager(eval_deps):
    """Thresholds low enough that the bundled month yields more than 8 findings; eval_deps has pipeline rows."""
    raw = copy.deepcopy(eval_deps.pack.raw)
    raw["briefing"]["thresholds"].update(mover_min_usd=1, mover_min_pct=1, mover_min_conversions=1,
                                         outlier_min_share_pct=1, outlier_cpa_pct=1, mix_gap_pts=1)
    return dataclasses.replace(eval_deps, pack=dataclasses.replace(eval_deps.pack, raw=raw))


def test_cards_are_ranked_by_stake_capped_and_chartable(eager, cfg):
    out = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))
    cards = out["cards"]
    assert 1 <= len(cards) <= insights.MAX_CARDS and out["writer"] == "templates"
    assert [c["stake"] for c in cards] == sorted((c["stake"] for c in cards), reverse=True)
    assert {"top movers", "efficiency outliers", "channel mix"} <= set(out["checked"])
    for c in cards:
        assert c["severity"] in ("high", "medium", "low") and c["title"] and c["headline"] and c["action"]
        assert len(c["facts"]) <= insights.FACTS_MAX
        if c["chart"] is not None:
            spec = PanelChartSpec(**c["chart"]["spec"])
            validate_spec(spec, list(c["chart"]["rows"][0]))


def test_insights_json_has_no_nan_or_inf(eager, cfg):
    json.dumps(insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0)), allow_nan=False)


def test_an_empty_window_has_no_cards(deps, cfg):
    out = insights.run_insights(deps, cfg, Filters(date(2023, 1, 1), date(2023, 1, 10)), None, TtlCache(0))
    assert out["cards"] == [] and out["problems"] == ["There is no ad data in this period."]


def test_a_failed_input_table_is_a_fixed_sentence(eager, cfg, monkeypatch):
    real = insights.execute

    def no_anomalies(deps, sql, **kw):
        if "fct_anomaly_flags" in sql:
            return SqlError(kind="SqlSchema", message="Table project.secret_dataset.fct_anomaly_flags not found")
        return real(deps, sql, **kw)

    monkeypatch.setattr(insights, "execute", no_anomalies)
    out = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))
    assert "couldn't read the anomalies table" in out["problems"]
    assert "secret_dataset" not in json.dumps(out) and out["cards"]


def test_a_failed_analysis_is_named_and_the_rest_render(eager, cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(insights.a, "top_movers", boom)
    out = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))
    assert "The top movers check could not run." in out["problems"] and out["cards"]
    assert "top movers" not in out["checked"]


def test_a_platform_narrows_every_card(eager, cfg):
    google = dataclasses.replace(WINDOW, categorical=(("platform", ("Google",)),))
    out = insights.run_insights(eager, cfg, google, None, TtlCache(0))
    assert all("Google" in c["headline"] or c["kind"] in ("anomaly",) for c in out["cards"])
    assert not any(c["kind"] in ("mix", "move") for c in out["cards"])


def test_results_are_cached_per_window(eager, cfg, monkeypatch):
    calls = []
    real = insights.execute
    monkeypatch.setattr(insights, "execute", lambda *a, **k: calls.append(1) or real(*a, **k))
    cache = TtlCache(60)
    insights.run_insights(eager, cfg, WINDOW, None, cache)
    n = len(calls)
    insights.run_insights(eager, cfg, WINDOW, None, cache)
    assert len(calls) == n


def test_top_movers_do_not_apply_before_the_data_starts(eager, cfg):
    """Final review I2: the previous 15 days of 2024-01-01..15 lie before the data, so nothing "started spending"."""
    out = insights.run_insights(eager, cfg, Filters(date(2024, 1, 1), date(2024, 1, 15)), None, TtlCache(0))
    assert "top movers" not in out["checked"] and "efficiency outliers" in out["checked"]
    assert [c for c in out["cards"] if c["kind"] == "mover"] == []


def test_a_truncated_gold_read_publishes_no_cards(eager, cfg, monkeypatch):
    """Final review M6: analysing an arbitrary subset of the rows would publish wrong dollar numbers."""
    monkeypatch.setattr(insights, "MAX_ROWS", 10)
    out = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))
    assert out["cards"] == [] and out["checked"] == []
    assert out["problems"] == ["the window is too long to analyse in full; narrow the dates"]


def test_no_findings_never_calls_the_writer(eager, cfg, monkeypatch):
    """Final review M7."""
    for name, empty in (("cost_items", ([],)), ("anomaly_items", ([],)), ("pacing", (None, [])), ("move_item", None),
                        ("top_movers", []), ("efficiency_outliers", []), ("mix_gaps", [])):
        monkeypatch.setattr(insights.a, name, lambda *a, _e=empty, **k: _e)

    def no_writer(*a, **k):
        raise AssertionError("the writer ran with nothing to write")

    monkeypatch.setattr(insights, "write", no_writer)
    out = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))
    assert out["cards"] == [] and out["checked"] and out["writer"] == "templates"
