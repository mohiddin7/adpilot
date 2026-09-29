"""Brief tests. Analyses run on small hand-built frames; the end-to-end tests use the bundled DuckDB data, a
FunctionModel for the writer and MemorySink for the audit trail and the follow-up memory."""

import dataclasses
import json
import math
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from adpilot.brief import DEFAULTS, run_brief, settings
from adpilot.brief import analyses as a
from adpilot.brief.render import esc, render
from adpilot.brief.writer import write
from adpilot.core.audit import MemorySink

T = dict(DEFAULTS)
LATEST = date(2026, 9, 24)
AS_OF = LATEST - timedelta(days=2)
WEEK = [AS_OF - timedelta(days=i) for i in range(7)]


def gold(days=45, plats=("Facebook", "Google", "TikTok"), conv=100):
    """Flat, healthy data: every platform spends $1,000 a day for 100 sales ($10 a sale)."""
    return pd.DataFrame([
        dict(date=LATEST - timedelta(days=i), platform=p, campaign_id=f"{p[:2]}1", campaign_name=f"{p}_Main",
             impressions=100_000.0, clicks=2000.0, spend=1000.0, conversions=float(conv))
        for i in range(days) for p in plats])


def tweak(df, plat, days, **cols):
    m = (df["platform"] == plat) & df["date"].isin(days)
    for c, v in cols.items():
        df.loc[m, c] = v
    return df


def flags(*rows):
    cols = ["date", "platform", "campaign_id", "campaign_name", "observed_cpa", "rolling_mean_cpa", "is_anomaly",
            "anomaly_direction", "confidence"]
    return pd.DataFrame([dict(zip(cols, r, strict=True)) for r in rows], columns=cols)


# ---------- the cost split ----------


@pytest.mark.parametrize("after", [
    dict(impressions=90_000, clicks=1500, spend=1300, conversions=60),
    dict(impressions=120_000, clicks=2600, spend=800, conversions=140),
    dict(impressions=100_000, clicks=2000, spend=1000, conversions=100),
])
def test_split_terms_sum_to_the_total_change(after):
    before = dict(impressions=100_000, clicks=2000, spend=1000, conversions=100)
    sp = a.split(before, after)
    cpa = (after["spend"] / after["conversions"]) / (before["spend"] / before["conversions"])
    assert math.isclose(sum(sp["terms"].values()), math.log(cpa), abs_tol=1e-12)


def test_split_is_undefined_without_clicks():
    assert a.split(dict(impressions=1, clicks=0, spend=1, conversions=1), dict(impressions=1, clicks=1, spend=1, conversions=1)) is None


@pytest.mark.parametrize("change, driver", [
    (dict(spend=1400.0), "price"),  # same views, clicks, sales; ads cost more
    (dict(clicks=1400.0, conversions=70.0), "clicks"),  # same buy rate, fewer clicks
    (dict(conversions=70.0), "buyers"),  # same clicks, fewer sales
])
def test_a_cost_rise_is_named_by_its_driver(change, driver):
    items, steady = a.cost_items(tweak(gold(), "Google", WEEK, **change), AS_OF, T)
    assert [i.id for i in items] == ["cost:Google"] and steady == ["Facebook", "TikTok"]
    i = items[0]
    assert a.CAUSES[driver][0] in i.checked and i.do == a.CAUSES[driver][2]
    assert i.stake > 0 and "per sale this week, up" in i.happened


def test_low_volume_never_makes_a_cost_item():
    df = tweak(gold(conv=3), "Google", WEEK, spend=2000.0)  # 21 sales a week < min_conversions
    assert a.cost_items(df, AS_OF, T) == ([], [])


# ---------- anomaly triage ----------


def test_sales_collapse_with_normal_clicks_is_a_tracking_fault_even_unflagged():
    df = tweak(gold(), "Google", [AS_OF - timedelta(days=2)], conversions=20.0)
    items, line = a.anomaly_items(df, flags(), AS_OF, T)
    assert [i.id for i in items] == ["anomaly:Google:Go1:tracking"] and line is None
    assert "20 sales from 2,000 clicks" in items[0].happened and "tracking" in items[0].checked


def test_sales_jump_with_normal_clicks_is_double_counting():
    df = tweak(gold(), "TikTok", [AS_OF], conversions=450.0)
    items, _ = a.anomaly_items(df, flags(), AS_OF, T)
    assert [i.id for i in items] == ["anomaly:TikTok:Ti1:double"]


def test_a_sales_drop_with_fewer_clicks_is_not_called_tracking():
    df = tweak(gold(), "Google", [AS_OF], conversions=20.0, clicks=400.0)
    assert a.anomaly_items(df, flags(), AS_OF, T)[0] == []


def test_small_one_day_flags_become_one_noise_line():
    d = AS_OF - timedelta(days=5)
    f = flags((d, "Google", "Go1", "Google_Main", 10.5, 10.0, 1, "HIGH_CPA", "high"),
              (d, "TikTok", "Ti1", "TikTok_Main", 11.0, 10.0, 1, "HIGH_CPA", "high"))
    items, line = a.anomaly_items(gold(), f, AS_OF, T)
    assert items == [] and line.startswith("2 other anomaly flags: small (under $101 each)")


def test_a_persistent_flag_over_100_dollars_needs_you():
    f = flags(*[(d, "Google", "Go1", "Google_Main", 12.0, 10.0, 1, "HIGH_CPA", "medium") for d in (AS_OF, AS_OF - timedelta(days=1))])
    items, _ = a.anomaly_items(gold(), f, AS_OF, T)
    assert [i.id for i in items] == ["anomaly:Google:Go1:high"]
    assert items[0].stake == pytest.approx(400) and "2 days this week" in items[0].confidence


def test_a_large_one_day_flag_needs_you():
    d = AS_OF - timedelta(days=5)
    f = flags((d, "Google", "Go1", "Google_Main", 20.0, 10.0, 1, "HIGH_CPA", "low"))
    items, _ = a.anomaly_items(gold(), f, AS_OF, T)
    assert items[0].stake == pytest.approx(1000) and items[0].confidence.startswith("low")


# ---------- pacing, outlook, staged move ----------


def forecast(plat="Google", spend=1000.0, conv=100.0, width=0.2, days=14):
    rows = []
    for i in range(1, days + 1):
        for m, v in (("spend", spend), ("conversions", conv)):
            rows.append(dict(forecast_execution_date=AS_OF, target_date=AS_OF + timedelta(days=i), platform=plat,
                             metric_name=m, predicted_value=v, lower_bound=v * (1 - width / 2), upper_bound=v * (1 + width / 2)))
    return pd.DataFrame(rows)


def test_overspend_is_an_item_and_underspend_only_a_line():
    budgets = {"Google": 20_000, "TikTok": 40_000}  # both spend $1,000 a day: $30K by month end
    lines, items, _ = a.pacing(gold(), None, budgets, LATEST, T)
    assert [i.id for i in items] == ["pace:Google:2026-09"]
    assert items[0].stake == pytest.approx(10_000) and "already spent its monthly budget" in items[0].do
    assert "Facebook: no budget set" in lines and "TikTok: will underspend by about $10K" in lines


def test_pacing_uses_the_forecast_for_the_rest_of_the_month():
    lines, _, off = a.pacing(gold(), forecast(spend=3000.0), {"Google": 30_000}, LATEST, T)
    assert off["Google"] == pytest.approx((24_000 + 6 * 3000) / 30_000 - 1)


def test_outlook_calls_a_direction_or_says_it_cannot():
    lines, series = a.outlook(gold(), forecast(spend=1300.0), AS_OF, T)
    assert lines == ["Google: cost per sale getting pricier ($10.00 now, about $13.00 forecast)"]
    assert series["platform"] == "Google" and len(series["points"]) == 28
    lines, series = a.outlook(gold(), forecast(width=1.0), AS_OF, T)
    assert lines == ["The forecast is too uncertain to call a direction for any platform"] and series is None


def plan(moves=(("Facebook", 20_000, 40_000, 6.0, 300), ("TikTok", 60_000, 40_000, 12.0, -150))):
    return pd.DataFrame([dict(platform=p, current_spend=c, recommended_spend=r, current_cpa=cpa,
                              current_conversions=c / cpa, conversion_delta=dc) for p, c, r, cpa, dc in moves])


def test_a_big_optimizer_move_becomes_a_staged_test():
    i = a.move_item(plan(), T)
    assert i.id == "move:TikTok>Facebook" and i.per == "a week"
    assert i.title == "Test moving $4.0K from TikTok to Facebook"  # 20 % of $20K
    assert "above $6.90" in i.do  # 1.15 × $6.00


def test_a_small_optimizer_move_is_ignored():
    assert a.move_item(plan((("Facebook", 50_000, 52_000, 6.0, 5), ("TikTok", 50_000, 48_000, 7.0, -3))), T) is None


# ---------- follow-up and ranking ----------


def stored(item_id, kind, first, check, stake=500.0):
    return {"item_id": item_id, "kind": kind, "advice": "Do it", "brief_date": first, "stake": stake, "check": check}


def test_follow_up_outcomes():
    df = gold()
    tweak(df, "Facebook", [d for d in df["date"].unique() if d > AS_OF - timedelta(days=3)], spend=0.0)
    rows = a.follow_up([
        stored("anomaly:Facebook:Fa1:high", "anomaly", AS_OF - timedelta(days=3),
               {"platform": "Facebook", "campaign_id": "Fa1", "kind": "high", "normal_spend": 1000.0}),
        stored("anomaly:Google:Go1:tracking", "anomaly", AS_OF - timedelta(days=3),
               {"platform": "Google", "campaign_id": "Go1", "kind": "tracking", "normal_spend": 1000.0}),
        stored("cost:TikTok", "cost", AS_OF - timedelta(days=2), {"platform": "TikTok", "cpa0": 10.0}),
        stored("pace:Google:2026-08", "pace", AS_OF - timedelta(days=10), {"platform": "Google", "month": "2026-08"}),
        stored("cost:Google", "cost", AS_OF - timedelta(days=20), {"platform": "Google", "cpa0": 10.0}),
        stored("cost:Facebook", "cost", AS_OF, {"platform": "Facebook", "cpa0": 10.0}),  # published today: not judged
    ], {}, df, AS_OF, LATEST, T)
    got = {r["item_id"]: (r["status"], r["outcome"]) for r in rows}
    assert got["anomaly:Facebook:Fa1:high"][0] == "done"
    assert got["anomaly:Google:Go1:tracking"] == ("resolved", "sales came back")
    assert got["cost:TikTok"] == ("resolved", "cost per sale is back to about $10.00")
    assert got["pace:Google:2026-08"] == ("expired", "the month is over")
    assert got["cost:Google"][0] == "expired" and "cost:Facebook" not in got


def test_an_open_item_stays_in_follow_up_unless_its_stake_grew():
    item = a.anomaly_items(tweak(gold(), "Google", [AS_OF], conversions=20.0), flags(), AS_OF, T)[0][0]
    followed = a.follow_up([stored(item.id, "anomaly", AS_OF - timedelta(days=1), item.check, stake=item.stake)],
                           {item.id: item}, gold(), AS_OF, LATEST, T)
    assert followed[0]["status"] == "open" and "day 1" in followed[0]["outcome"]
    assert a.rank([item], followed, T) == ([], [])
    followed[0]["stored_stake"] = item.stake / 2
    assert a.rank([item], followed, T)[0] == [item]


def test_rank_keeps_the_top_three_by_stake():
    items = [dataclasses.replace(a.move_item(plan(), T), id=f"x{n}", stake=n) for n in range(5)]
    top, rest = a.rank(items, [], T)
    assert [i.id for i in top] == ["x4", "x3", "x2"] and [i.id for i in rest] == ["x1", "x0"]


# ---------- writer ----------


def item():
    return a.anomaly_items(tweak(gold(), "Google", [AS_OF], conversions=20.0), flags(), AS_OF, T)[0][0]


def writer_model(out):
    def fn(messages, info):
        if isinstance(out, Exception):
            raise out
        return ModelResponse(parts=[ToolCallPart("final_result", out)])

    return FunctionModel(fn)


def test_the_writer_keeps_grounded_slots_and_templates_the_rest(deps):
    i = item()
    out = {"headline": "1 thing needs you today.", "story": "Google lost 80 sales to tracking.",
           "items": [{"item_id": i.id, "title": "Fix Google tracking now",
                      "checked": "Clicks were steady at 2,000 but sales fell to 20.",
                      "do": "Check the pixel; it has cost 999 dollars."}]}
    text, used, caveats = write(deps, writer_model(out), [i], [], [], "run1")
    assert text["headline"] == "1 thing needs you today." and text[i.id]["title"] == "Fix Google tracking now"
    assert text[i.id]["checked"].startswith("Clicks were steady")
    assert text[i.id]["do"] == i.do  # 999 is in no fact
    assert text["story"] == ""  # 80 is in no fact either
    assert caveats == ["BriefTemplated: 2 of 5 slot(s) from templates"]
    rec = deps.audit.calls[-1]
    assert rec.run_id == "run1" and rec.case_name == "writer" and rec.source == "brief"


def test_the_writer_cleans_links_mentions_and_headings(deps):
    i = item()
    out = {"headline": "## @team see https://evil.example now", "story": "", "items": [
        {"item_id": i.id, "title": "**Fix** [tracking](http://x.y)", "checked": i.checked, "do": i.do}]}
    text, _, _ = write(deps, writer_model(out), [i], [], [], "r")
    assert text["headline"] == "team see now"
    assert write(deps, writer_model({**out, "headline": "Fix Always_On_2026_04 today."}), [i], [], [], "r")[0][
        "headline"] == "Fix Always_On_2026_04 today."  # names keep their underscores
    assert not any(c in text[i.id]["title"] for c in ("http", "*", "[", "]"))


def test_an_unknown_item_id_gets_templates(deps):
    i = item()
    out = {"headline": "1 thing needs you today.", "story": "", "items": [
        {"item_id": "made-up", "title": "x", "checked": "x", "do": "x"}]}
    text, _, _ = write(deps, writer_model(out), [i], [], [], "r")
    assert text[i.id] == {"title": i.title, "checked": i.checked, "do": i.do}


def test_a_failed_writer_means_templates_and_a_caveat(deps):
    i = item()
    text, used, caveats = write(deps, writer_model(RuntimeError("down")), [i], [], [], "r")
    assert used is None and caveats[0].startswith("BriefWriterFailed")
    assert text["headline"] == "1 thing needs you today. Everything else was checked." and text[i.id]["do"] == i.do
    assert deps.audit.calls[-1].confidence == 0.0


# ---------- render ----------


def test_campaign_names_cannot_inject_markdown_links_or_mentions():
    s = esc('Promo_[click](https://evil.example) @everyone # <b>x</b>')
    assert "\\[click\\]" in s and "@everyone" not in s and "https://" not in s and "<b>" not in s


def test_the_issue_body_stays_under_githubs_limit():
    big = dataclasses.replace(item(), numbers=[{"date": str(n), "spend": "x" * 200} for n in range(400)])
    text = {"headline": "h", "story": "", big.id: {"title": big.title, "checked": big.checked, "do": big.do}}
    md = render(as_of=AS_OF, latest=LATEST, items=[big], text=text, ahead=[], series=None, followed=[], fine=[],
                warnings=[], footer="f")
    assert len(md) < 65_536 and "<details>" not in md


# ---------- settings and end to end ----------


def with_briefing(deps, briefing):
    deps.pack = dataclasses.replace(deps.pack, raw={**deps.pack.raw, "briefing": briefing})
    return deps


@pytest.mark.parametrize("briefing, bad", [
    ({"thresholds": {"max_itmes": 3}}, "unknown"),
    ({"thresholds": {"max_items": -1}}, "positive"),
    ({"budgets": {"Google": "lots"}}, "positive"),
    ({"questions": ["old style"]}, "unknown"),
])
def test_a_bad_briefing_block_is_an_error(deps, briefing, bad):
    with pytest.raises(ValueError, match=bad):
        settings(with_briefing(deps, briefing).pack)


def test_the_brief_on_bundled_data_stores_its_items_and_follows_up_next_day(deps):
    first = run_brief(deps, None, now=datetime(2024, 1, 31, tzinfo=UTC))
    assert first.problems == [] and first.markdown.startswith("# Daily brief · week ending")
    assert "writer `templates`" in first.markdown
    assert {r.item_id for r in first.rows} == {i.id for i in first.items} and first.items
    # The next day's brief reads them back: pretend they were published a day earlier.
    deps.audit.brief_items = [r.model_copy(update={"brief_date": r.brief_date - timedelta(days=1)}) for r in first.rows]
    second = run_brief(deps, None, now=datetime(2024, 2, 1, tzinfo=UTC))
    assert "## Following up" in second.markdown
    assert all(r.status in ("open", "resolved", "done", "expired") for r in second.rows)


def test_a_memory_read_failure_is_a_problem_not_a_silent_gap(deps):
    class Broken(MemorySink):
        def recent_brief_items(self, pack, since):
            raise RuntimeError("bq down")

    deps.audit = Broken()
    b = run_brief(deps, None)
    assert b.problems == ["couldn't load past advice (bq down)"] and "⚠ couldn't load past advice" in b.markdown


def test_a_missing_input_table_is_shown_and_fails_the_job(deps):
    real = deps.connector

    class NoAnomalies:
        dialect = real.dialect

        def query(self, sql, max_bytes=None):
            if "fct_anomaly_flags" in sql:
                raise RuntimeError("table gone")
            return real.query(sql, max_bytes)

    deps.connector = NoAnomalies()
    b = run_brief(deps, None)
    assert b.problems == ["couldn't read the flags table (table gone)"] and "⚠ couldn't read the flags table" in b.markdown


def test_no_data_is_said_plainly(deps):
    class Empty:
        dialect = "duckdb"

        def query(self, sql, max_bytes=None):
            return pd.DataFrame({"d": [None]})

    deps.connector = Empty()
    b = run_brief(deps, None)
    assert b.problems == ["the ad data is empty"] and "No brief today" in b.markdown


def test_the_writer_model_words_the_published_brief(deps):
    def fn(messages, info):
        facts = json.loads(next(p.content for p in messages[0].parts if p.part_kind == "user-prompt"))
        return ModelResponse(parts=[ToolCallPart("final_result", {
            "headline": f"{facts['needs_you_count']} things need you today.", "story": "",
            "items": [{"item_id": i["item_id"], "title": "Plain words title", "checked": "Plain words cause.",
                       "do": "Plain words move."} for i in facts["items"]]})])

    b = run_brief(deps, FunctionModel(fn))
    assert "Plain words title" in b.markdown and "writer `function" in b.markdown
    assert b.rows[0].advice == "Plain words title"


def test_pace_numbers_projects_month_end_from_the_run_rate():
    from datetime import date, timedelta

    import pandas as pd

    from adpilot.brief.analyses import pace_numbers

    latest = date(2026, 9, 10)
    rows = [{"date": latest - timedelta(days=i), "platform": "Google", "spend": 100.0} for i in range(15)]
    rows.append({"date": latest, "platform": "Bing", "spend": 5.0})
    out = pace_numbers(pd.DataFrame(rows), None, {"Google": 3000}, latest)
    g = out["Google"]
    assert g["spent_mtd"] == 1000.0  # September 1-10
    assert g["days_left"] == 20
    assert g["projected"] == 1000.0 + 20 * 100.0  # 7-day run rate is $100/day
    assert g["basis"] == "last 7 days" and g["budget"] == 3000.0
    assert out["Bing"]["budget"] is None and out["Bing"]["projected"] is None


# ---------- window-vs-window analyses (the dashboard's /insights) ----------


def camp(rows):
    """rows: (platform, campaign_id, spend, conversions) → one day of gold rows."""
    return pd.DataFrame([dict(date=LATEST, platform=p, campaign_id=c, campaign_name=f"{c}_name", impressions=1e5,
                              clicks=2e3, spend=float(s), conversions=float(v)) for p, c, s, v in rows])


def test_top_movers_ranks_by_dollars_and_counts_new_and_stopped_campaigns():
    prev = camp([("Google", "g1", 1000, 100), ("Google", "g2", 5000, 100), ("TikTok", "t1", 2000, 50)])
    cur = camp([("Google", "g1", 1050, 100), ("Google", "g2", 9000, 100), ("Facebook", "f1", 3000, 60)])
    items = a.top_movers(cur, prev, T)
    assert [i.id for i in items] == ["mover:Google:g2:spend", "mover:Google:g2:cpa", "mover:Facebook:f1:spend"]
    assert [i.stake for i in items] == pytest.approx([4000, (90 - 50) * 100, 3000])  # a stable sort keeps ties in order
    assert all(i.stake >= T["mover_min_usd"] for i in items)
    assert "mover:Google:g1:spend" not in {i.id for i in items}  # +5 % and $50: below both thresholds


def test_top_movers_needs_enough_sales_for_a_cpa_move():
    prev, cur = camp([("Google", "g1", 1000, 5)]), camp([("Google", "g1", 1000, 2)])
    assert [i.id for i in a.top_movers(cur, prev, T)] == []


def test_efficiency_outliers_flag_expensive_campaigns_with_real_spend():
    cur = camp([("Google", "g1", 10_000, 1000), ("Google", "g2", 4000, 100), ("TikTok", "t1", 100, 1)])
    items = a.efficiency_outliers(cur, T)
    assert [i.id for i in items] == ["outlier:Google:g2"]  # t1 is pricier but under 5 % of spend
    account = 14_100 / 1101
    assert items[0].stake == pytest.approx(4000 - account * 100)


def test_efficiency_outliers_zero_sales_is_an_outlier_without_a_cpa():
    items = a.efficiency_outliers(camp([("Google", "g1", 10_000, 1000), ("Google", "g2", 2000, 0)]), T)
    assert items[0].check["cpa"] is None and "no sales" in items[0].happened
    assert items[0].stake == pytest.approx(2000)


def test_mix_gaps_flag_platforms_whose_spend_share_outruns_their_sales_share():
    cur = camp([("Google", "g1", 6000, 100), ("TikTok", "t1", 4000, 300)])
    items = a.mix_gaps(cur, T)
    assert [i.id for i in items] == ["mix:Google"]
    assert "60%" in items[0].happened and "25%" in items[0].happened


def test_mix_gaps_needs_two_platforms():
    assert a.mix_gaps(camp([("Google", "g1", 6000, 100)]), T) == []


def test_new_analyses_with_zero_conversions_everywhere_return_nothing():
    cur = camp([("Google", "g1", 6000, 0), ("TikTok", "t1", 4000, 0)])
    assert a.efficiency_outliers(cur, T) == [] and a.mix_gaps(cur, T) == []
    assert all(i.id.endswith(":spend") for i in a.top_movers(cur, camp([("Google", "g1", 1000, 0)]), T))


def test_all_values_equal_flags_nothing():
    same = camp([("Google", "g1", 1000, 100), ("TikTok", "t1", 1000, 100)])
    assert a.top_movers(same, same, T) == [] and a.efficiency_outliers(same, T) == [] and a.mix_gaps(same, T) == []


def test_writer_records_the_callers_source(deps):
    item = a.mix_gaps(camp([("Google", "g1", 6000, 100), ("TikTok", "t1", 4000, 300)]), T)[0]
    fn = FunctionModel(lambda m, info: ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
        "headline": "One thing.", "items": [], "story": ""})]))
    write(deps, fn, [item], [], [], "run1", source="dashboard", case_name="insights_writer")
    rec = deps.audit.calls[-1]
    assert (rec.source, rec.case_name) == ("dashboard", "insights_writer")
