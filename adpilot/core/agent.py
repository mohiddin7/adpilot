"""The one agent. `ask()` is the entry point every surface (CLI, API, MCP) calls."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    RunContext,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
    UsageLimits,
    capture_run_messages,
)
from pydantic_ai._agent_graph import (
    get_captured_run_messages,  # ponytail: private; test_stops_after_three_sql_executions breaks if it moves
)
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage

from adpilot.core.audit import build_record, new_trace_id, primary_model_name, summarize_messages
from adpilot.core.chart import ChartSpec, heuristic_chart
from adpilot.core.errors import AdPilotError, ErrorKind
from adpilot.core.guardrails import Budget, classify_question, is_in_scope, redact_output, sanitize_question
from adpilot.core.models import NoNullSchemas, after, daily_cap
from adpilot.core.tools import AgentDeps, SqlError, SqlResult, execute, records, register_tools

log = logging.getLogger(__name__)
Agent.instrument_all()

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
        capabilities=[NoNullSchemas()],
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


def refused(answer: AnalystAnswer) -> bool:
    """True for a model Refusal, an out-of-scope block, or a guard rejection (sanitize_question)."""
    blocked_by_guard = answer.caveats[:1] == ["InputPolicy"] and answer.confidence == 0.0
    return "OutOfScope" in answer.caveats or answer.answer_md == REFUSAL_TEXT or blocked_by_guard


FALLBACK_NOTE = "answered without the language model"


def fell_back(answer: AnalystAnswer) -> bool:
    """True when the rule-based path answered instead of the model — a canned query (confidence 0.3) or an apology."""
    return any(FALLBACK_NOTE in c for c in answer.caveats)


def ask(
    agent: Agent[AgentDeps, AnalystAnswer | Refusal],
    deps: AgentDeps,
    question: str,
    history: Sequence[ModelMessage] | None = None,
    model: Model | str | None = None,
    event_stream_handler=None,
) -> tuple[AnalystAnswer, list[ModelMessage], str]:
    """Guard → run → map failures. Never raises for model/data problems; returns (answer, new_messages, trace_id).

    Every exit path buffers one AuditRecord in deps.audit (no I/O here — the caller flushes).
    event_stream_handler is forwarded to run_sync so a streaming surface needs no second run path; the rule-based fallback emits no events because it makes no model call."""
    trace_id = new_trace_id()
    started, t0 = datetime.now(UTC), time.perf_counter()
    asked = question
    model_requested = primary_model_name(model) or primary_model_name(agent.model)
    guard_caveats: list[str] = []  # e.g. GuardDegraded when layer 1 was unreachable and the answer ran on layer 0 alone
    first: list[ModelMessage] = []   # a failed first attempt: audited, never returned as history
    second: list[ModelMessage] = []
    rerun: list[str] = []            # ["Rerun: <kind>"] once the one re-run has started
    n_history = len(history or [])   # capture_run_messages() lists the history first; the audit wants this turn only

    def done(answer: AnalystAnswer, messages: list[ModelMessage], usage=None) -> tuple[AnalystAnswer, list[ModelMessage], str]:
        answer.answer_md, leaked = redact_output(answer.answer_md)
        if leaked:
            answer.caveats.append("OutputPolicy")
        answer.caveats.extend(c for c in guard_caveats + rerun if c not in answer.caveats)
        # messages_json is the next turn's history (load_session), so it holds only what ask() returns; the failed
        # attempt goes to the usage totals and to attributes["rerun"].
        tried = first[n_history:]
        deps.audit.record(build_record(
            trace_id=trace_id, ts=started, latency_s=time.perf_counter() - t0, question=asked, answer_md=answer.answer_md,
            sql=answer.sql, refused=refused(answer), confidence=answer.confidence, caveats=answer.caveats, messages=messages,
            usage=_usage_of([*tried, *(messages or second[n_history:])]) if rerun else usage, model_requested=model_requested,
            context=deps.run_context, pack_name=deps.pack.name, prompt_hash=deps.pack.prompt_hash,
            extra_attributes={"rerun": summarize_messages(tried).model_dump()} if rerun else None,
        ))
        return answer, messages, trace_id

    try:
        question = sanitize_question(question)  # layer 0
        if not is_in_scope(question):
            return done(AnalystAnswer(answer_md=REFUSAL_TEXT, confidence=1.0, caveats=["OutOfScope"]), [])
        guard_caveats = classify_question(question)  # layer 1 (off unless ADPILOT_INPUT_CLASSIFIER is set)
    except AdPilotError as exc:
        return done(AnalystAnswer(answer_md=exc.message, confidence=0.0, caveats=[exc.kind] + ([exc.layer] if exc.layer else [])), [])

    deps.budget = Budget()
    deps.results.clear()
    if model is None and agent.model is None:
        return done(_rule_based(deps, question, "ModelUnavailable", "No model API key configured"), [])

    try:
        try:
            with _capture() as first:
                result = _run(agent, deps, question, history, model, event_stream_handler)
        except (UsageLimitExceeded, UnexpectedModelBehavior) as exc:
            # A quality failure, not an outage (outages were handled inside the chain): one re-run on the next model.
            rest = after(model or agent.model, _answered_by(first[n_history:]))
            if rest is None:
                raise
            rerun.append(f"Rerun: {_classify(exc)[0]}")
            log.warning("agent run failed (%s); re-running once on the next model", rerun[0])
            deps.budget = Budget()
            deps.results.clear()
            with _capture() as second:
                result = _run(agent, deps, f"{question}\n\n{_failure_note(exc, first[n_history:])}", history, rest, event_stream_handler)
    except Exception as exc:  # noqa: BLE001 — every failure mode maps to a typed fallback
        kind, detail = _classify(exc)
        log.warning("agent run failed (%s): %s", kind, detail[:300])
        return done(_rule_based(deps, question, kind, detail), [])

    out = result.output
    usage = result.usage() if callable(result.usage) else result.usage
    if isinstance(out, Refusal):
        return done(AnalystAnswer(answer_md=REFUSAL_TEXT, confidence=1.0, caveats=["OutOfScope", out.reason]), result.new_messages(), usage)
    if out.data is None and deps.last_result is not None:
        out.data = records(deps.last_result.head(deps.pack.max_result_rows))
    return done(out, result.new_messages(), usage)


def _classify(exc: Exception) -> tuple[ErrorKind, str]:
    if isinstance(exc, ExceptionGroup):
        inner = exc.exceptions
        if inner and all(isinstance(e, ModelHTTPError) and e.status_code == 429 for e in inner):
            return "ModelRateLimited", "; ".join(str(e) for e in inner)
        return "ModelUnavailable", "; ".join(str(e) for e in inner)
    if isinstance(exc, ModelHTTPError):
        cap = daily_cap(exc)
        return ("ModelRateLimited" if exc.status_code == 429 else "ModelUnavailable"), (f"{cap}; {exc}" if cap else str(exc))
    if isinstance(exc, ModelAPIError):
        return "ModelUnavailable", str(exc)
    if isinstance(exc, UsageLimitExceeded):
        return "BudgetExceeded", str(exc)
    if isinstance(exc, UnexpectedModelBehavior):
        return "ModelUnavailable", str(exc)
    if isinstance(exc, AdPilotError):
        return exc.kind, exc.message
    return "ModelUnavailable", f"{exc.__class__.__name__}: {exc}"


@contextmanager
def _capture():
    """capture_run_messages(), still visible to a caller's own capture around ask(): pydantic-ai gives a run only to
    the innermost context, so without this ask() would silently empty it (first run only, as pydantic-ai does)."""
    try:
        outer = get_captured_run_messages()
    except LookupError:
        outer = None
    with capture_run_messages() as messages:
        try:
            yield messages
        finally:
            if outer is not None and not outer.messages:
                outer.messages.extend(messages)


def _run(agent, deps: AgentDeps, prompt: str, history, model, event_stream_handler):
    return agent.run_sync(
        prompt,
        deps=deps,
        message_history=list(history) if history else None,
        model=model,
        usage_limits=UsageLimits(request_limit=MAX_MODEL_CALLS),
        event_stream_handler=event_stream_handler,
    )


def _answered_by(messages: Sequence[ModelMessage]) -> str | None:
    return next((m.model_name for m in reversed(messages) if isinstance(m, ModelResponse) and m.model_name), None)


def _failure_note(exc: Exception, messages: Sequence[ModelMessage]) -> str:
    """Trusted text we author; it skips the input guards, which already ran on the question it follows."""
    if isinstance(exc, UsageLimitExceeded):
        note = ("Note: a previous attempt failed (BudgetExceeded) — it used every allowed model call without answering. "
                "Use at most two tool calls, then give the final answer.")
    else:
        note = f"Note: a previous attempt failed (invalid output: {str(exc)[:200]}). Return the final answer in the required schema."
    # The SQL budget resets for the re-run, so a BudgetExceeded return says nothing useful; the error before it does.
    errors = [p.content for m in messages if isinstance(m, ModelRequest) for p in m.parts
              if p.part_kind == "tool-return" and isinstance(p.content, SqlError) and p.content.kind != "BudgetExceeded"]
    if errors:
        e = errors[-1]
        note += f" Its last query error was {e.kind}: {e.message[:200]} — {e.hint}."
    return note


def _usage_of(messages: Sequence[ModelMessage]) -> RunUsage:
    """Both attempts' usage, summed from their responses (a failed run has no RunResult to ask)."""
    usage = RunUsage()
    for m in messages:
        if isinstance(m, ModelResponse):
            usage.requests += 1
            usage.incr(m.usage)
    return usage


def _rule_based(deps: AgentDeps, question: str, kind: ErrorKind, detail: str) -> AnalystAnswer:
    """No model: answer from the pack's canned queries when the question matches one."""
    caveat = f"{kind}: {FALLBACK_NOTE} ({detail[:160]})."
    deps.budget = Budget()
    deps.results.clear()
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
