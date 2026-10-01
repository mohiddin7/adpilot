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
    rank = [(not c["loss"], -c["stake"]) for c in cards]  # money lost first, then the rest; by stake within each
    assert rank == sorted(rank)
    campaigns = [tuple(c["id"].split(":")[1:3]) for c in cards if c["kind"] in ("anomaly", "mover", "outlier")]
    assert len(campaigns) == len(set(campaigns))  # one card per campaign
    assert out["at_stake"] == round(sum(c["stake"] for c in cards if c["loss"]), 2)
    assert {"top movers", "efficiency outliers", "channel mix"} <= set(out["checked"])
    for c in cards:
        assert c["severity"] in ("high", "medium", "low") and c["title"] and c["headline"] and c["action"]
        assert len(c["facts"]) <= insights.FACTS_MAX
        if c["chart"] is not None:
            spec = PanelChartSpec(**c["chart"]["spec"])
            validate_spec(spec, list(c["chart"]["rows"][0]))
            assert not {"value", "name", "period", "series", "measure"} & {spec.y}  # every y axis has a real name


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


def _item(id, stake, happened="", **check):
    """A finding as an analysis would return it; `check` carries what its evidence chart reads."""
    return insights.a.Item(id=id, kind=id.split(":")[0], subject="s", stake=stake, happened=happened or f"{id} happened.",
                           title="t", checked="c", do="d", confidence="high", check_line="", check=check)


def _only(monkeypatch, items):
    """Every analysis finds nothing, except that `items` come back from one that always runs."""
    for name, empty in (("cost_items", ([],)), ("anomaly_items", ([],)), ("pacing", (None, [])), ("move_item", None),
                        ("top_movers", []), ("mix_gaps", [])):
        monkeypatch.setattr(insights.a, name, lambda *a, _e=empty, **k: _e)
    monkeypatch.setattr(insights.a, "efficiency_outliers", lambda *a, **k: list(items))


def _outlier(cid, stake, platform="Google"):
    return _item(f"outlier:{platform}:{cid}", stake, platform=platform, campaign_id=cid, cpa=20.0, account=10.0)


def _mover(cid, metric, stake, platform="Google"):
    return _item(f"mover:{platform}:{cid}:{metric}", stake, platform=platform, campaign_id=cid, metric=metric,
                 before=10.0, after=20.0)


def test_each_kind_says_whether_its_stake_is_money_lost(eager, cfg, monkeypatch):
    """Live pass, finding 9: a change in spend and an amount to reallocate are not losses."""
    _only(monkeypatch, [
        _item("cost:Google", 8, platform="Google"), _item("anomaly:Google:a1:high", 7, platform="Google", campaign_id="a1"),
        _item("pace:Google:2024-01", 6, platform="Google"), _outlier("o1", 5), _mover("m1", "cpa", 4),
        _mover("m2", "spend", 3), _item("move:TikTok>Facebook", 2), _item("mix:TikTok", 1, platform="TikTok")])
    cards = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))["cards"]
    assert {c["id"]: (c["loss"], c["stake_label"]) for c in cards} == {
        "cost:Google": (True, "at stake"), "anomaly:Google:a1:high": (True, "at stake"),
        "pace:Google:2024-01": (True, "at stake"), "outlier:Google:o1": (True, "at stake"),
        "mover:Google:m1:cpa": (True, "at stake"), "mover:Google:m2:spend": (False, "change in spend"),
        "move:TikTok>Facebook": (False, "to reallocate"), "mix:TikTok": (False, "to reallocate")}
    assert all(c["also"] == [] for c in cards)


def test_a_smaller_loss_ranks_above_a_larger_reallocation_and_only_losses_are_at_stake(eager, cfg, monkeypatch):
    _only(monkeypatch, [_item("mix:TikTok", 9000.004, platform="TikTok"), _mover("m2", "spend", 5000),
                        _outlier("o1", 100.126), _outlier("o2", 300.25)])
    out = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))
    assert [c["id"] for c in out["cards"]] == ["outlier:Google:o2", "outlier:Google:o1", "mix:TikTok", "mover:Google:m2:spend"]
    assert out["at_stake"] == 400.38  # 300.25 + 100.13: not the 9,000 to reallocate, not the 5,000 change in spend


def test_no_loss_findings_put_nothing_at_stake(eager, cfg, monkeypatch):
    _only(monkeypatch, [_item("mix:TikTok", 9000, platform="TikTok")])
    assert insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))["at_stake"] == 0


def test_a_campaign_that_is_an_outlier_and_a_mover_is_one_card_and_frees_a_slot(eager, cfg, monkeypatch):
    """Live pass, finding 9: the same campaign showed twice. The duplicate's sentence moves to `also`, before the cut."""
    twice = _item("mover:Google:c1:cpa", 800, "It also got pricier.", platform="Google", campaign_id="c1", metric="cpa",
                  before=10.0, after=20.0)
    others = [_outlier(f"c{n}", 900 - 100 * n) for n in range(2, 9)]  # c2 (700) … c8 (100): c8 is the ninth finding
    _only(monkeypatch, [_outlier("c1", 900), twice, *others, _outlier("c1", 50, platform="TikTok")])
    cards = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))["cards"]
    assert len(cards) == insights.MAX_CARDS
    assert [c["id"] for c in cards][:2] == ["outlier:Google:c1", "outlier:Google:c2"]
    assert cards[0]["also"] == ["It also got pricier."]
    assert "mover:Google:c1:cpa" not in [c["id"] for c in cards]
    assert cards[-1]["id"] == "outlier:Google:c8" and cards[-1]["also"] == []  # the freed slot went to the next finding
    assert "outlier:TikTok:c1" not in [c["id"] for c in cards]  # same campaign id on another platform: its own key, cut ninth


@pytest.mark.parametrize("metric", ["spend", "cpa"])
def test_a_mover_chart_names_its_y_axis(eager, cfg, monkeypatch, metric):
    """Live pass, finding 7: the axis said "Value"."""
    _only(monkeypatch, [_mover("m1", metric, 500)])
    chart = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))["cards"][0]["chart"]
    assert chart["spec"]["y"] == metric and chart["formats"] == {metric: "currency"}
    assert chart["rows"] == [{"period": "previous", metric: 10.0}, {"period": "this", metric: 20.0}]
