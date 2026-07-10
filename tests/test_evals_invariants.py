from adpilot.core.agent import AnalystAnswer
from evals.invariants import RULES, evaluate_invariants
from evals.task import Trace


def t(rows):
    return Trace(answer=AnalystAnswer(answer_md="", data=rows))


def test_rule_names():
    assert [r.name for r in RULES] == [
        "platform_spend_sums_to_total", "platform_conversions_sum_to_total", "worst_cpa_in_ranking",
        "top_spend_platform_consistent", "anomaly_platforms_exist", "budget_totals_balance",
    ]


def test_sum_rule_pass_and_fail(eval_duck, pack):
    traces = {
        "total_spend": t([{"spend": 130244.9}]),
        "spend_by_platform": t([{"platform": "TikTok", "spend": 74266.7}, {"platform": "Google", "spend": 37686.2}, {"platform": "Facebook", "spend": 18292.0}]),
    }
    res = {r.name: r for r in evaluate_invariants(traces, eval_duck, pack)}
    assert res["platform_spend_sums_to_total"].passed is True
    traces["total_spend"] = t([{"spend": 999.0}])
    res = {r.name: r for r in evaluate_invariants(traces, eval_duck, pack)}
    assert res["platform_spend_sums_to_total"].passed is False


def test_missing_case_is_skipped(eval_duck, pack):
    res = {r.name: r for r in evaluate_invariants({}, eval_duck, pack)}
    assert all(r.passed is None for r in res.values())


def test_top_platform_escapes_quotes_in_campaign_name(eval_duck, pack):
    """Regression test: the campaign name is interpolated into SQL unescaped. A name containing a
    single quote used to break the generated SQL; it must no longer raise, and the rule reports a
    result (pass or fail on the merits) instead of an 'error: ...' reason."""
    traces = {
        "spend_by_platform": t([{"platform": "TikTok", "spend": 100.0}, {"platform": "Google", "spend": 50.0}]),
        "top_spend_campaign": t([{"campaign_name": "O'Brien's Ads", "spend": 100.0}]),
    }
    res = {r.name: r for r in evaluate_invariants(traces, eval_duck, pack)}
    result = res["top_spend_platform_consistent"]
    assert result.passed in (True, False)
    assert not result.reason.startswith("error")


def test_membership_and_budget_rules(eval_duck, pack):
    traces = {
        "worst_cpa_campaign": t([{"platform": "Google", "campaign_name": "Search_Generic_Terms", "cpa": 24.8}]),
        "cpa_ranking": t([{"platform": "Google", "campaign_name": "Search_Generic_Terms", "cpa": 24.8}, {"platform": "TikTok", "campaign_name": "X", "cpa": 1}]),
        "spend_by_platform": t([{"platform": "TikTok", "spend": 74266.7}, {"platform": "Google", "spend": 37686.2}]),
        "top_spend_campaign": t([{"campaign_name": "Influencer_Collab", "spend": 26312.3}]),
        "anomalies_list": t([{"platform": "Google", "campaign_name": "Search_Generic_Terms"}]),
        "budget_plan": t([{"platform": "TikTok", "current_spend": 74266.7, "recommended_spend": 80000.0}, {"platform": "Google", "current_spend": 37686.2, "recommended_spend": 30000.0}, {"platform": "Facebook", "current_spend": 18292.0, "recommended_spend": 20244.9}]),
    }
    res = {r.name: r for r in evaluate_invariants(traces, eval_duck, pack)}
    assert res["worst_cpa_in_ranking"].passed is True
    assert res["top_spend_platform_consistent"].passed is True  # TikTok is top platform; Influencer_Collab is a TikTok campaign
    assert res["anomaly_platforms_exist"].passed is True
    assert res["budget_totals_balance"].passed is True
