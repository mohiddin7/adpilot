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


def test_cards_put_losses_first_one_per_campaign_capped_and_chartable(eager, cfg):
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


def _mover(cid, metric, stake, platform="Google", before=10.0, after=20.0):
    return _item(f"mover:{platform}:{cid}:{metric}", stake, platform=platform, campaign_id=cid, metric=metric,
                 before=before, after=after)


def _anomaly(cid, kind, stake, platform="Google"):
    return _item(f"anomaly:{platform}:{cid}:{kind}", stake, platform=platform, campaign_id=cid, kind=kind)


def test_each_kind_says_whether_its_stake_is_money_lost(eager, cfg, monkeypatch):
    """Live pass, finding 9: a change in spend and an amount to reallocate are not losses."""
    _only(monkeypatch, [
        _item("cost:Google", 8, platform="Google"), _anomaly("a1", "high", 7),
        _item("pace:Google:2024-01", 6, platform="Google"), _outlier("o1", 5), _mover("m1", "cpa", 4),
        _mover("m2", "spend", 3), _item("move:TikTok>Facebook", 2), _item("mix:TikTok", 1, platform="TikTok")])
    cards = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))["cards"]
    assert {c["id"]: (c["loss"], c["stake_label"]) for c in cards} == {
        "cost:Google": (True, "at stake"), "anomaly:Google:a1:high": (True, "at stake"),
        "pace:Google:2024-01": (True, "at stake"), "outlier:Google:o1": (True, "at stake"),
        "mover:Google:m1:cpa": (True, "at stake"), "mover:Google:m2:spend": (False, "change in spend"),
        "move:TikTok>Facebook": (False, "to reallocate"), "mix:TikTok": (False, "to reallocate")}
    assert all(c["also"] == [] for c in cards)


def test_an_improvement_is_an_opportunity_not_a_loss(eager, cfg, monkeypatch):
    """Review R2: a cost per sale that fell, and a day cheaper than usual, are titled "Consider more budget for …"."""
    _only(monkeypatch, [_mover("m1", "cpa", 900, before=20.0, after=10.0), _anomaly("a1", "low", 800),
                        _anomaly("a2", "tracking", 70), _anomaly("a3", "double", 60), _outlier("o1", 50)])
    out = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))
    assert {c["id"]: (c["loss"], c["stake_label"]) for c in out["cards"]} == {
        "mover:Google:m1:cpa": (False, "opportunity"), "anomaly:Google:a1:low": (False, "opportunity"),
        "anomaly:Google:a2:tracking": (True, "at stake"), "anomaly:Google:a3:double": (False, "to verify"),
        "outlier:Google:o1": (True, "at stake")}
    assert [c["id"] for c in out["cards"]][:2] == ["anomaly:Google:a2:tracking", "outlier:Google:o1"]
    # 70 + 50: neither the 1,700 of opportunity nor the 60 of double-counted sales (owner, round 3) is money lost
    assert out["at_stake"] == 120.0


def test_a_real_loss_is_not_folded_under_the_same_campaigns_improvement(eager, cfg, monkeypatch):
    """Review R2: cost per sale fell from $60 to $30 against a $10 account average. Still a campaign to cut or fix."""
    better = _item("mover:Google:c1:cpa", 3000, "It got cheaper.", platform="Google", campaign_id="c1", metric="cpa",
                   before=60.0, after=30.0)
    _only(monkeypatch, [better, _item("outlier:Google:c1", 2000, platform="Google", campaign_id="c1", cpa=30.0, account=10.0)])
    cards = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))["cards"]
    assert [c["id"] for c in cards] == ["outlier:Google:c1"] and cards[0]["loss"] and cards[0]["also"] == ["It got cheaper."]


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


# ---------- the engine-computed "why" (round 2): hand-built frames, exact numbers ----------

AS_OF = date(2024, 1, 30)


def _rows(*rows):
    """(date, platform, campaign_id, impressions, clicks, spend, conversions) → the gold frame's shape."""
    import pandas as pd

    return pd.DataFrame([{"date": d, "platform": p, "campaign_id": c, "campaign_name": f"{c} name", "impressions": i,
                          "clicks": k, "spend": s, "conversions": v} for d, p, c, i, k, s, v in rows])


PREV = _rows((date(2024, 1, 10), "Google", "g1", 1000, 100, 100.0, 10), (date(2024, 1, 10), "Google", "g2", 1000, 100, 100.0, 0),
             (date(2024, 1, 10), "TikTok", "t1", 5000, 500, 100.0, 100))
CUR = _rows((date(2024, 1, 25), "Google", "g1", 1500, 75, 180.0, 15), (date(2024, 1, 25), "Google", "g2", 1000, 100, 100.0, 10),
            (date(2024, 1, 25), "TikTok", "t1", 5000, 500, 100.0, 100))
# g1: ad price 0.10 → 0.12 a view (+20%), clicks per view 0.10 → 0.05 (−50%), sales per click 0.10 → 0.20 (+100%)
G1_CHANGES = [{"measure": "Ad price", "change": 0.2}, {"measure": "Clicks per view", "change": -0.5},
              {"measure": "Sales per click", "change": 1.0}]


def _why(item, cur=CUR, prev=PREV, gold=None):
    import pandas as pd

    return insights._why(item, pd.concat([prev, cur]) if gold is None else gold, cur, prev, AS_OF)


@pytest.mark.parametrize("item", [_mover("g1", "cpa", 1), _anomaly("g1", "high", 1)])
def test_a_campaigns_cost_per_sale_why_is_its_rate_split_against_the_previous_period(item):
    why = _why(item)
    assert why["chart"]["spec"] == PanelChartSpec(chart_type="bar", x="measure", y="change").model_dump()
    assert why["chart"]["rows"] == G1_CHANGES and why["chart"]["formats"] == {"change": "percent"}
    assert why["text"] == ("Ad price +20%, clicks per view -50%, sales per click +100%. Mostly fewer people clicked, "
                           "which points to worn-out ad creative.")


def test_a_zero_rate_has_no_chart_and_says_there_is_no_single_cause():
    why = _why(_mover("g2", "cpa", 1))  # g2 had no sales in the previous period
    assert why == {"text": "The change has no single clear cause in ad price, clicks or sales per click.", "chart": None}


def test_a_spend_movers_why_splits_views_from_price_per_view():
    why = _why(_mover("g1", "spend", 1))  # views 1,000 → 1,500, price per view 0.10 → 0.12
    assert why["chart"]["rows"] == [{"measure": "Views", "change": 0.5}, {"measure": "Price per view", "change": 0.2}]
    assert why["chart"]["formats"] == {"change": "percent"} and why["chart"]["spec"]["chart_type"] == "bar"
    assert why["text"] == "Views +50%, price per view +20%. The change comes mostly from more views."


def test_a_spend_mover_from_nothing_has_no_split():
    started = _rows((date(2024, 1, 25), "Google", "g9", 1000, 100, 100.0, 10))
    why = insights._why(_mover("g9", "spend", 1), started, started, PREV, AS_OF)
    assert why["chart"] is None and "can't be split" in why["text"]


def test_an_outliers_why_compares_its_rates_with_the_accounts():
    why = _why(_outlier("g1", 1))
    # the account: 7,500 views, 675 clicks, $380, 125 sales. g1: 1,500 views, 75 clicks, $180, 15 sales.
    assert why["chart"]["rows"] == [{"measure": "Ad price", "vs_account": 1.3684},
                                    {"measure": "Clicks per view", "vs_account": -0.4444},
                                    {"measure": "Sales per click", "vs_account": 0.08}]
    assert why["chart"]["spec"]["y"] == "vs_account" and why["chart"]["formats"] == {"vs_account": "percent"}
    assert why["text"] == ("Against the account: ad price +137%, clicks per view -44%, sales per click +8%. "
                           "Mostly its ads cost more per view.")  # the price term alone carries over half the gap


def test_a_platforms_cost_why_is_its_weekly_rate_split_and_names_the_campaign_driving_it():
    import pandas as pd

    last = _rows((date(2024, 1, 20), "Google", "g1", 1000, 100, 100.0, 10), (date(2024, 1, 20), "TikTok", "t1", 5000, 500, 100.0, 100))
    this = _rows((date(2024, 1, 27), "Google", "g1", 1500, 75, 180.0, 15), (date(2024, 1, 27), "TikTok", "t1", 5000, 500, 100.0, 100))
    old = _rows((date(2024, 1, 10), "Google", "g5", 1000, 100, 900.0, 1))  # in the window, before both weeks
    why = insights._why(_item("cost:Google", 1, platform="Google"), pd.concat([old, last, this]), pd.concat([old, last, this]),
                        PREV, AS_OF)  # the 7 days to Jan 30 vs the 7 before
    assert why["chart"]["rows"] == G1_CHANGES
    assert why["text"].startswith("Ad price +20%, clicks per view -50%, sales per click +100%. Mostly fewer people clicked")
    # the two weeks only: g5 (Jan 10) is not named; g1 = 280 − 25 × (480 / 225) over the account's two weeks
    assert why["text"].endswith('"g1 name" carries 100% of Google\'s cost above the account average ($227 of $227).')


MANY = _rows(*[(date(2024, 1, 25), "Google", f"g{n}", 1000, 100, spend, 10)
               for n, spend in enumerate((700.0, 600.0, 500.0, 400.0, 300.0, 200.0, 10.0), start=1)],
             (date(2024, 1, 25), "TikTok", "t1", 5000, 500, 100.0, 100))


@pytest.mark.parametrize("item", [_item("mix:Google", 1, platform="Google"), _item("pace:Google:2024-01", 1, platform="Google")])
def test_a_platforms_why_is_its_campaigns_by_excess_cost(item):
    """Excess = spend − sales × the account's cost per sale (2,810 / 170). Largest first, positive only, at most 5."""
    account = 2810 / 170
    why = _why(item, cur=MANY)
    assert why["chart"]["spec"] == PanelChartSpec(chart_type="bar_h", x="campaign_name", y="excess_cost").model_dump()
    assert why["chart"]["formats"] == {"excess_cost": "currency"}
    assert why["chart"]["rows"] == [{"campaign_name": f"g{n} name", "excess_cost": round(spend - 10 * account, 2)}
                                    for n, spend in ((1, 700), (2, 600), (3, 500), (4, 400), (5, 300))]
    total = sum(spend - 10 * account for spend in (700, 600, 500, 400, 300, 200))  # g6 counts; g7 is below the account
    assert why["text"] == (f'"g1 name" carries {(700 - 10 * account) / total:.0%} of Google\'s cost above the account '
                           f"average ($535 of $1.7K).")


def test_a_platform_with_no_campaign_above_the_account_says_so():
    assert _why(_item("mix:TikTok", 1, platform="TikTok"), cur=MANY) == {
        "text": "No TikTok campaign pays more per sale than the account average.", "chart": None}


def test_a_budget_move_has_no_why():
    assert _why(_item("move:TikTok>Facebook", 1)) is None


def test_every_card_carries_a_why_detail_whose_chart_is_drawable(eager, cfg):
    cards = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))["cards"]
    assert cards and any(c["why_detail"] for c in cards)
    for c in cards:
        why = c["why_detail"]  # the key is always there
        assert c["platform"] in ("Facebook", "Google", "TikTok", None)
        if why is not None:
            assert why["text"] and "**" not in why["text"]
            if why["chart"] is not None:
                validate_spec(PanelChartSpec(**why["chart"]["spec"]), list(why["chart"]["rows"][0]))


def test_a_move_card_has_no_why_detail_and_no_platform(eager, cfg, monkeypatch):
    _only(monkeypatch, [_item("move:TikTok>Facebook", 5), _outlier("o1", 9)])
    cards = {c["kind"]: c for c in insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))["cards"]}
    assert cards["move"]["why_detail"] is None and cards["move"]["platform"] is None
    assert cards["outlier"]["platform"] == "Google"


def test_a_why_that_fails_leaves_its_card_in_place(eager, cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(insights, "_why", boom)
    cards = insights.run_insights(eager, cfg, WINDOW, None, TtlCache(0))["cards"]
    assert cards and all(c["why_detail"] is None for c in cards) and all(c["headline"] for c in cards)


# ---------- round 3: a cost per sale that fell is explained in the cheaper direction ----------

BEFORE = {"impressions": 1000.0, "clicks": 100.0, "spend": 100.0, "conversions": 10.0}
RISING = [phrase for what, cause, _ in insights.a.CAUSES.values() for phrase in (what, cause)]


def test_a_falling_cost_per_sale_names_the_cheaper_cause_never_a_rising_one():
    """Review round 3: ad price halved read "Mostly ads got pricier, which points to more competition"."""
    checked, _ = insights.a.explain(insights.a.split(BEFORE, {**BEFORE, "spend": 50.0}))
    assert checked == ("Ad price -50%, clicks per view +0%, sales per click +0%. Mostly ads got cheaper, "
                       "which points to less competition in the ad auction.")
    assert not any(phrase in checked for phrase in RISING)
    two = insights.a.explain(insights.a.split(BEFORE, {**BEFORE, "clicks": 200.0, "conversions": 20.0}))[0]
    assert two.endswith("Mostly more people clicked, which points to creative that is landing well.")
    assert not any(phrase in two for phrase in RISING)


def test_a_rising_cost_per_sale_reads_as_before():
    checked, do = insights.a.explain(insights.a.split(BEFORE, {**BEFORE, "spend": 200.0}))
    assert checked.endswith("Mostly ads got pricier, which points to more competition in the ad auction.")
    assert do == "Review bids, or wait a few days for prices to settle."


def test_a_campaign_whose_cost_per_sale_fell_gets_the_cheaper_why():
    cur = _rows((date(2024, 1, 25), "Google", "g1", 1000, 100, 50.0, 10))
    prev = _rows((date(2024, 1, 10), "Google", "g1", 1000, 100, 100.0, 10))
    why = insights._why(_mover("g1", "cpa", 1, before=10.0, after=5.0), pd_concat(prev, cur), cur, prev, AS_OF)
    assert why["text"].endswith("Mostly ads got cheaper, which points to less competition in the ad auction.")
    assert why["chart"]["rows"][0] == {"measure": "Ad price", "change": -0.5}


def pd_concat(*frames):
    import pandas as pd

    return pd.concat(frames)


def test_an_outlier_with_a_zero_rate_says_no_rate_explains_the_gap():
    cur = _rows((date(2024, 1, 25), "Google", "g8", 1000, 100, 100.0, 0), (date(2024, 1, 25), "TikTok", "t1", 5000, 500, 100.0, 100))
    why = insights._why(_outlier("g8", 1), cur, cur, PREV, AS_OF)
    assert why == {"text": "No single rate explains the gap with the account.", "chart": None}
