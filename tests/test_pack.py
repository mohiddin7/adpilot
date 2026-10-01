import pytest

from adpilot.connectors import get_connector
from adpilot.packs.loader import load_pack


def test_load_ads_pack():
    pack = load_pack("ads")
    assert set(pack.tables) == {"gold", "anomalies", "budget", "forecast"}
    assert "Cost per Acquisition" in pack.glossary
    assert "run_sql" in pack.system_prompt


def test_bigquery_ref_uses_env(monkeypatch):
    monkeypatch.setenv("BQ_PROJECT_ID", "p1")
    monkeypatch.setenv("BQ_PRODUCTION_DATASET", "prod1")
    pack = load_pack("ads")
    assert pack.table_ref("gold", "bigquery") == "p1.prod1.fct_unified_marketing_performance"
    assert pack.table_ref("gold", "duckdb") == "fct_unified_marketing_performance"
    assert "p1.prod1.fct_unified_marketing_performance" in pack.allowed_tables("bigquery")


def test_render_substitutes_refs():
    pack = load_pack("ads")
    assert pack.render("SELECT 1 FROM {gold}", "duckdb") == "SELECT 1 FROM fct_unified_marketing_performance"


def test_duckdb_gold_matches_pipeline_totals():
    pack = load_pack("ads")
    src = get_connector("duckdb", pack)
    df = src.query("SELECT platform, ROUND(SUM(spend), 2) AS s FROM fct_unified_marketing_performance GROUP BY 1 ORDER BY 1")
    assert dict(zip(df["platform"], df["s"], strict=True)) == {
        "Facebook": 18292.0,
        "Google": 37686.2,
        "TikTok": 74266.7,
    }
    assert {"cpa", "roas", "engagement_rate"} <= {c for c, _ in src.columns("fct_unified_marketing_performance")}
    assert src.query("SELECT COUNT(*) AS n FROM tbl_forecast")["n"][0] == 0


@pytest.mark.parametrize("logical", ["anomalies", "budget", "forecast"])
def test_tier2_docs_match_duckdb_columns(pack, duck, logical):
    documented = list(pack.tables[logical]["columns"])
    physical = [c for c, _ in duck.columns(pack.table_ref(logical, "duckdb"))]
    assert documented == physical


def test_anomaly_direction_values_documented(pack):
    assert "HIGH_CPA" in pack.tables["anomalies"]["columns"]["anomaly_direction"]


def test_briefing_block_is_valid(pack):
    """The brief refuses to run on a typo'd threshold, so the shipped pack must pass its own check."""
    from adpilot.brief import settings

    t, budgets = settings(pack)
    assert set(budgets) == {"Facebook", "Google", "TikTok"} and t["max_items"] == 3


@pytest.mark.parametrize("bad", [0, -1, "soon", None])
def test_a_bad_query_timeout_fails_at_load(pack, bad):
    import copy
    import dataclasses

    raw = copy.deepcopy(pack.raw)
    raw["query_timeout_s"] = bad
    with pytest.raises(ValueError, match="query_timeout_s"):
        dataclasses.replace(pack, raw=raw).query_timeout_s  # noqa: B018 — the property's validation is the point


def test_the_ads_pack_times_queries_out_at_30_seconds(pack):
    assert pack.query_timeout_s == 30
