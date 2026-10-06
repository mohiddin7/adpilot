import math

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


@pytest.mark.parametrize("bad", [0, -1, "soon", None, math.nan, math.inf, 0.5])
def test_a_bad_query_timeout_fails_at_load(pack, bad):
    import copy
    import dataclasses

    raw = copy.deepcopy(pack.raw)
    raw["query_timeout_s"] = bad
    with pytest.raises(ValueError, match="query_timeout_s"):
        dataclasses.replace(pack, raw=raw).query_timeout_s  # noqa: B018 — the property's validation is the point


def test_the_ads_pack_times_queries_out_at_30_seconds(pack):
    assert pack.query_timeout_s == 30


def _pack_sql(raw) -> list[tuple[str, str]]:
    """Every SQL string in the pack, with a label: any dict that carries an `sql` key, at any depth."""
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            if isinstance(node.get("sql"), str):
                found.append((str(node.get("id") or node.get("title") or path), node["sql"]))
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(raw, "pack")
    return found


def test_no_pack_query_aggregates_one_of_its_own_output_names(pack):
    """BigQuery resolves a name in HAVING / ORDER BY / QUALIFY to the SELECT alias first, so `SUM(conversions) AS
    conversions ... HAVING SUM(conversions) > 0` is SUM(SUM(...)): "Aggregations of aggregations are not allowed".
    DuckDB reads the table column, so only a live BigQuery run would catch it (it did: the efficiency map, 2026-10-01).
    Qualify the column (`FROM {gold} AS g ... SUM(g.conversions)`) or filter in an outer query.

    A static check, so it does not catch: an expression argument such as `SUM(conversions * 1)`, an aggregate other
    than SUM/AVG/MIN/MAX/COUNT, or an alias written without `AS`."""
    import re

    queries = _pack_sql(pack.raw)
    assert len(queries) > 20  # the walk found the panels and the fallback queries, not an empty list
    offenders = []
    for label, sql in queries:
        aliases = {a.lower() for a in re.findall(r"\bAS\s+([A-Za-z_]\w*)", sql, re.IGNORECASE)}
        for clause in re.findall(r"\b(?:HAVING|QUALIFY|ORDER\s+BY)\b(.*?)(?=\b(?:HAVING|QUALIFY|ORDER\s+BY|LIMIT|UNION)\b|$)",
                                 sql, re.IGNORECASE | re.DOTALL):
            for name in re.findall(r"\b(?:SUM|AVG|MIN|MAX|COUNT)\s*\(\s*(?:DISTINCT\s+)?([A-Za-z_]\w*)\s*\)", clause,
                                   re.IGNORECASE):
                if name.lower() in aliases:
                    offenders.append((label, name))
    assert offenders == []


def _bq_sqls():
    from adpilot.dashboard.config import load_dashboard

    pack = load_pack("ads")
    return [(p.id, p.sql) for p in load_dashboard(pack).panels] + [
        (f"fallback_{i}", fq["sql"]) for i, fq in enumerate(pack.raw.get("fallback_queries", []))]


@pytest.mark.parametrize("name,sql", _bq_sqls())
def test_pack_sql_passes_the_guardrails_in_the_bigquery_dialect(name, sql):
    from adpilot.core.guardrails import validate_sql

    pack = load_pack("ads")
    validate_sql(pack.render(sql, "bigquery", where="1 = 1"), pack.allowed_tables("bigquery"), 2001,
                 dialect="bigquery")
