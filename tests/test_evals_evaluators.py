from pydantic_evals.evaluators import EvaluatorContext

from adpilot.core.agent import AnalystAnswer
from evals.cases import Expected
from evals.evaluators import (
    ExecutionAccuracy,
    Factual,
    Refuses,
    SafeSql,
    Trajectory,
    ValueWithinTolerance,
    numbers_in,
    rows_equivalent,
)
from evals.task import EvalInputs, Trace

GOLD = "fct_unified_marketing_performance"


def ctx(trace, expected, family="factual", tool=None):
    return EvaluatorContext(
        name="c", inputs=EvalInputs(name="c", question="q"), metadata={"family": family, "tool": tool, "group": None, "consistency": False},
        expected_output=expected, output=trace, duration=0.1, _span_tree=None, attributes={}, metrics={},
    )


def trace(md="", sql=None, executed=(), attempted=(), calls=("run_sql",), model_calls=2, repairs=0, refused=False):
    return Trace(answer=AnalystAnswer(answer_md=md, sql=sql), sql_executed=list(executed), sql_attempted=list(attempted),
                 tool_calls=list(calls), model_calls=model_calls, repairs=repairs, refused=refused)


def test_rows_equivalent_order_and_superset_and_tolerance():
    exp = [{"platform": "A", "spend": 100.0}, {"platform": "B", "spend": 50.0}]
    ok, _ = rows_equivalent(exp, [{"platform": "B", "spend": 50.2, "extra": 1}, {"platform": "A", "spend": 100.0, "extra": 2}], 0.01)
    assert ok
    ok, why = rows_equivalent(exp, [{"platform": "A", "spend": 100.0}], 0.01)
    assert not ok and "row count" in why
    ok, _ = rows_equivalent(exp, [{"platform": "A", "spend": 130.0}, {"platform": "B", "spend": 50.0}], 0.01)
    assert not ok


def test_rows_equivalent_positional_when_names_differ():
    assert rows_equivalent([{"n": 12}], [{"campaigns": 12}], 0.01)[0]


def test_numbers_in_handles_money_and_percent():
    assert numbers_in("Spend was $130.2K on 12 campaigns (5.6x ROAS, 25.97%)") == [130200.0, 12, 5.6, 25.97]


def test_execution_accuracy_against_duckdb(eval_duck, pack):
    exp = Expected(sql="SELECT platform, ROUND(SUM(spend), 2) AS spend FROM {gold} GROUP BY platform")
    good = f"SELECT platform, ROUND(SUM(spend), 2) AS spend FROM {GOLD} GROUP BY platform ORDER BY spend"
    assert ExecutionAccuracy(eval_duck, pack).evaluate(ctx(trace(sql=good), exp))["execution"].value is True
    bad = f"SELECT platform, SUM(clicks) AS spend FROM {GOLD} GROUP BY platform"
    assert ExecutionAccuracy(eval_duck, pack).evaluate(ctx(trace(sql=bad), exp))["execution"].value is False
    assert ExecutionAccuracy(eval_duck, pack).evaluate(ctx(trace(sql=None), exp))["execution"].value is False
    assert ExecutionAccuracy(eval_duck, pack).evaluate(ctx(trace(), exp, family="redteam")) == {}


def test_execution_accuracy_rejects_unsafe_sql(eval_duck, pack):
    """Regression test: model-written SQL used to be executed on DuckDB without validate_sql first."""
    exp = Expected(sql="SELECT platform, ROUND(SUM(spend), 2) AS spend FROM {gold} GROUP BY platform")
    unsafe = f"DELETE FROM {GOLD}"
    result = ExecutionAccuracy(eval_duck, pack).evaluate(ctx(trace(sql=unsafe), exp))["execution"]
    assert result.value is False
    assert "unsafe" in result.reason


def test_value_within_tolerance():
    exp = Expected(sql="x", value=130244.9, column="spend")
    assert ValueWithinTolerance().evaluate(ctx(trace(md="Total spend was $130,244.90."), exp)) == {"value": True}
    assert ValueWithinTolerance().evaluate(ctx(trace(md="About $130.2K"), exp)) == {"value": True}  # K-suffix
    assert ValueWithinTolerance().evaluate(ctx(trace(md="$99"), exp)) == {"value": False}
    assert ValueWithinTolerance().evaluate(ctx(trace(md="x"), Expected(sql="x"))) == {}


def test_factual_is_execution_or_value(eval_duck, pack):
    exp = Expected(sql="SELECT ROUND(SUM(spend), 2) AS spend FROM {gold}", value=130244.9, column="spend")
    f = Factual(eval_duck, pack)
    assert f.evaluate(ctx(trace(md="$130.2K in total", sql="SELECT 1 AS nope FROM " + GOLD), exp))["factual"].value is True
    assert f.evaluate(ctx(trace(md="unknown", sql=None), exp))["factual"].value is False
    assert f.evaluate(ctx(trace(md="unknown", sql=None), exp, family="scope")) == {}


def test_refuses():
    assert Refuses().evaluate(ctx(trace(refused=True), Expected(refuse=True), family="redteam")) == {"refusal": True}
    assert Refuses().evaluate(ctx(trace(refused=True), Expected(refuse=False), family="scope")) == {"refusal": False}
    assert Refuses().evaluate(ctx(trace(), Expected(sql="x"))) == {}


def test_safe_sql(pack):
    ok = [f"SELECT platform FROM {GOLD} LIMIT 10"]
    assert SafeSql(pack).evaluate(ctx(trace(executed=ok), Expected(sql="x"))) == {"safe_sql": True}
    assert SafeSql(pack).evaluate(ctx(trace(executed=ok + [f"DELETE FROM {GOLD}"]), Expected(sql="x"))) == {"safe_sql": False}
    assert SafeSql(pack).evaluate(ctx(trace(executed=[]), Expected(refuse=True), family="redteam")) == {"safe_sql": True}


def test_trajectory():
    t = Trajectory()
    r = t.evaluate(ctx(trace(attempted=["a", "b"], calls=["run_sql", "run_sql"]), Expected(sql="x")))
    assert r == {"calls_ok": True, "sql_ok": True, "no_loop": True}
    r = t.evaluate(ctx(trace(attempted=["a", "a"], model_calls=5), Expected(sql="x")))
    assert r["no_loop"] is False and r["calls_ok"] is False
    r = t.evaluate(ctx(trace(calls=["run_sql"]), Expected(sql="x"), tool="get_anomalies"))
    assert r["tool_ok"] is False
    assert t.evaluate(ctx(trace(refused=True, model_calls=0), Expected(refuse=True), family="redteam")) == {}
