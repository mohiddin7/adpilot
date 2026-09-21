"""Tier 1: a scripted model per case. Proves the harness and the agent plumbing without any API call."""

from __future__ import annotations

from typing import Any

from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import FunctionModel

from evals.cases import ACCURACY_FAMILIES, EvalCase, Expected

TOTAL_SPEND = Expected(sql="SELECT ROUND(SUM(spend), 2) AS spend FROM {gold}", value=130244.9, column="spend")
SYNTHETIC: list[EvalCase] = [
    EvalCase(name="synthetic_repair", family="factual", question="total spend (repair path)", expected=TOTAL_SPEND),
    EvalCase(name="synthetic_fallback_429", family="factual", question="total spend (fallback path)", expected=TOTAL_SPEND),
]
_SCOPE_SQL = "SELECT platform, ROUND(SUM(spend), 2) AS spend FROM {gold} GROUP BY platform ORDER BY spend DESC"
_TOOL_ARGS = {"get_anomalies": {}, "get_budget_plan": {}, "get_forecast": {"platform": "TikTok"}}


def _tool_returns(messages):
    return [p for p in messages[-1].parts if p.part_kind == "tool-return"]


def _final(answer_md: str, sql: str | None) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("final_result_AnalystAnswer", {"answer_md": answer_md, "sql": sql})])


def _answer_text(case: EvalCase, result: Any) -> str:
    if case.expected.value is not None:
        return f"The answer is {case.expected.value:g}."
    rows = getattr(result, "rows", None) or []
    return f"Scripted answer with {len(rows)} rows." if rows else "Scripted answer."


def _scripted(case: EvalCase, pack: Any, dialect: str, first_sql: str | None = None) -> FunctionModel:
    """Turn 1: call the tool (or run_sql). After a tool return: final answer. Repairs once if the return is an error."""
    sql = pack.render(case.expected.sql or _SCOPE_SQL, dialect)

    def fn(messages, info):
        returns = _tool_returns(messages)
        if not returns:
            if case.tool:
                return ModelResponse(parts=[ToolCallPart(case.tool, _TOOL_ARGS.get(case.tool, {}))])
            return ModelResponse(parts=[ToolCallPart("run_sql", {"sql": first_sql or sql})])
        res = returns[0].content
        if res.__class__.__name__ == "SqlError":
            return ModelResponse(parts=[ToolCallPart("run_sql", {"sql": sql})])
        return _final(_answer_text(case, res), getattr(res, "sql", None))

    return FunctionModel(fn)


def _refusal() -> FunctionModel:
    return FunctionModel(lambda messages, info: ModelResponse(parts=[ToolCallPart("final_result_Refusal", {"reason": "off topic"})]))


def _rate_limited(messages, info):
    raise ModelHTTPError(429, "primary", body={"error": "rate limited"})


def model_for(case: EvalCase, pack: Any, dialect: str = "duckdb") -> Model:
    if case.name == "synthetic_repair":
        bad = pack.render("SELECT platfrm, SUM(spend) AS spend FROM {gold} GROUP BY platfrm", dialect)
        return _scripted(case, pack, dialect, first_sql=bad)
    if case.name == "synthetic_fallback_429":
        return FallbackModel(FunctionModel(_rate_limited, model_name="primary"), _scripted(case, pack, dialect))
    if case.expected.guard:  # the model would comply; only layer 0 can make the case pass
        return _scripted(case, pack, dialect)
    if case.family in ("redteam", "scope") and case.expected.refuse:
        return _refusal()
    if case.family in ACCURACY_FAMILIES or case.family in ("narrative", "scope"):
        return _scripted(case, pack, dialect)
    raise ValueError(f"no script for {case.name}")
