"""The one agent. `ask()` is the entry point every surface (CLI, API, MCP) calls."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext, UnexpectedModelBehavior, UsageLimitExceeded, UsageLimits
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

from adpilot.core.chart import ChartSpec, heuristic_chart
from adpilot.core.errors import AdPilotError, ErrorKind
from adpilot.core.guardrails import Budget, is_in_scope, sanitize_question
from adpilot.core.tools import AgentDeps, SqlResult, execute, records, register_tools

log = logging.getLogger(__name__)

MAX_MODEL_CALLS = 4


class AnalystAnswer(BaseModel):
    answer_md: str
    sql: str | None = None
    data: list[dict] | None = None
    chart: ChartSpec | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    caveats: list[str] = []


class Refusal(BaseModel):
    """The question is not about this data set."""

    reason: str


def build_agent(model: Model | str | None = None) -> Agent[AgentDeps, AnalystAnswer | Refusal]:
    agent: Agent[AgentDeps, AnalystAnswer | Refusal] = Agent(
        model,
        deps_type=AgentDeps,
        output_type=[AnalystAnswer, Refusal],
        retries=2,
        name="adpilot",
    )

    @agent.instructions
    def instructions(ctx: RunContext[AgentDeps]) -> str:
        pack = ctx.deps.pack
        return f"{pack.system_prompt}\n\n# Tables\n{ctx.deps.schema_text}\n\n{pack.glossary}"

    register_tools(agent)
    return agent


REFUSAL_TEXT = (
    "I can only answer questions about this marketing performance data — spend, conversions, "
    "campaigns, anomalies, forecasts and budget recommendations."
)


def ask(
    agent: Agent[AgentDeps, AnalystAnswer | Refusal],
    deps: AgentDeps,
    question: str,
    history: Sequence[ModelMessage] | None = None,
    model: Model | str | None = None,
) -> tuple[AnalystAnswer, list[ModelMessage]]:
    """Guard → run → map failures. Never raises for model/data problems; returns (answer, new_messages)."""
    try:
        question = sanitize_question(question)
    except AdPilotError as exc:
        return AnalystAnswer(answer_md=exc.message, confidence=0.0, caveats=[exc.kind]), []
    if not is_in_scope(question):
        return AnalystAnswer(answer_md=REFUSAL_TEXT, confidence=1.0, caveats=["OutOfScope"]), []

    deps.budget = Budget()
    if model is None and agent.model is None:
        return _rule_based(deps, question, "ModelUnavailable", "No model API key configured"), []

    try:
        result = agent.run_sync(
            question,
            deps=deps,
            message_history=list(history) if history else None,
            model=model,
            usage_limits=UsageLimits(request_limit=MAX_MODEL_CALLS),
        )
    except Exception as exc:  # noqa: BLE001 — every failure mode maps to a typed fallback
        kind, detail = _classify(exc)
        log.warning("agent run failed (%s): %s", kind, detail[:300])
        return _rule_based(deps, question, kind, detail), []

    out = result.output
    if isinstance(out, Refusal):
        return AnalystAnswer(answer_md=REFUSAL_TEXT, confidence=1.0, caveats=["OutOfScope", out.reason]), result.new_messages()
    if out.data is None and deps.last_result is not None:
        out.data = records(deps.last_result.head(deps.pack.max_result_rows))
    return out, result.new_messages()


def _classify(exc: Exception) -> tuple[ErrorKind, str]:
    if isinstance(exc, ExceptionGroup):
        inner = exc.exceptions
        if inner and all(isinstance(e, ModelHTTPError) and e.status_code == 429 for e in inner):
            return "ModelRateLimited", "; ".join(str(e) for e in inner)
        return "ModelUnavailable", "; ".join(str(e) for e in inner)
    if isinstance(exc, ModelHTTPError):
        return ("ModelRateLimited" if exc.status_code == 429 else "ModelUnavailable"), str(exc)
    if isinstance(exc, ModelAPIError):
        return "ModelUnavailable", str(exc)
    if isinstance(exc, UsageLimitExceeded):
        return "BudgetExceeded", str(exc)
    if isinstance(exc, UnexpectedModelBehavior):
        return "ModelUnavailable", str(exc)
    if isinstance(exc, AdPilotError):
        return exc.kind, exc.message
    return "ModelUnavailable", f"{exc.__class__.__name__}: {exc}"


def _rule_based(deps: AgentDeps, question: str, kind: ErrorKind, detail: str) -> AnalystAnswer:
    """No model: answer from the pack's canned queries when the question matches one."""
    caveat = f"{kind}: answered without the language model ({detail[:160]})."
    deps.budget = Budget()
    for fq in deps.pack.raw.get("fallback_queries", []):
        if all(re.search(k, question, re.IGNORECASE) for k in fq["keywords"]):
            res = execute(deps, deps.pack.render(fq["sql"], deps.connector.dialect))
            if isinstance(res, SqlResult):
                numeric = [c for c in res.columns if res.rows and isinstance(res.rows[0].get(c), int | float)]
                return AnalystAnswer(
                    answer_md=f"{fq['title']} (from a pre-defined query).",
                    sql=res.sql,
                    data=res.rows,
                    chart=heuristic_chart(res.columns, numeric),
                    confidence=0.3,
                    caveats=[caveat],
                )
    return AnalystAnswer(
        answer_md="The analysis model is unavailable right now and no pre-defined query matches this question. Please try again shortly.",
        confidence=0.0,
        caveats=[caveat],
    )
