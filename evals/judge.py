"""LLM judge: separate model chain from the agent, rubric-based verdicts, and a calibration set that checks the judge."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio
import yaml
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from adpilot.core.audit import (
    AuditSink,
    RunContextInfo,
    build_record,
    new_trace_id,
    primary_model_name,
    summarize_messages,
)
from adpilot.core.models import (
    ROUTER,
    NoNullSchemas,
    agent_chain_from_env,
    build_chain,
    chain_from,
    openrouter_factory,
    renamed_env,
    same_model,
)
from evals.cases import ACCURACY_FAMILIES, Expected, reference_rows
from evals.evaluators import Factual
from evals.task import Trace

# Live probe of every tool-capable free model, 2026-09-23 (docs/models.md): 3/3 calibration agreement, fastest first.
# None is in the agent's chain (judge_config enforces it). openrouter/free is in both: a verdict it routes to an agent
# model is discarded in judge_answer.
DEFAULT_JUDGE_CHAIN = (
    "nex-agi/nex-n2.5-mini:free",
    "dots-studio/dots-3-note-preview:free",
    "cohere/north-mini-code:free",
    "qwen/qwen3.8-27b:free",
    ROUTER,
)
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1"
JUDGE_RELIABLE_MIN = 0.8
JUDGE_PASS_MIN = 0.75


@dataclass
class JudgeConfig:
    endpoint: str
    api_key: str | None
    models: list[str]
    agent_models: list[str]  # the agent's chain: a verdict answered by one of these is discarded


def judge_config(env: Mapping[str, str] | None = None) -> JudgeConfig:
    """Raises ValueError when the judge and agent chains share a model other than openrouter/free (fail closed)."""
    env = os.environ if env is None else env
    endpoint = (renamed_env("JUDGE_LLM_ENDPOINT_URL", "LLM_JUDGE_ENDPOINT_URL", "LLM_ENDPOINT_URL", env=env) or DEFAULT_ENDPOINT).rstrip("/")
    if endpoint.endswith("/chat/completions"):
        endpoint = endpoint[: -len("/chat/completions")]
    # JUDGE_LLM_MODELS overrides DEFAULT_JUDGE_CHAIN; the old split names are ignored, with a warning
    models = chain_from(renamed_env(
        "JUDGE_LLM_MODELS", "JUDGE_LLM_TARGET_MODEL", "JUDGE_LLM_FALLBACK_MODEL", "LLM_JUDGE_TARGET_MODEL", "LLM_JUDGE_FALLBACK_MODEL", env=env
    ), DEFAULT_JUDGE_CHAIN)
    agent_models = agent_chain_from_env(env)
    base = lambda names: {n.removesuffix(":free") for n in names} - {ROUTER}  # noqa: E731
    if shared := sorted(base(models) & base(agent_models)):
        raise ValueError(f"the judge chain shares {', '.join(shared)} with the agent chain: a model must not grade its own answers")
    return JudgeConfig(
        endpoint=endpoint,
        # the judge may borrow the agent's key, but never the agent's model
        api_key=renamed_env("JUDGE_LLM_BEARER_TOKEN", "LLM_JUDGE_BEARER_TOKEN", env=env)
        or renamed_env("AGENT_LLM_BEARER_TOKEN", "OPENROUTER_API_KEY", "LLM_BEARER_TOKEN", env=env)
        or None,
        models=models,
        agent_models=agent_models,
    )


def build_judge_model(cfg: JudgeConfig) -> Model | None:
    if not cfg.api_key:
        return None
    if "openrouter.ai" in cfg.endpoint:
        make = openrouter_factory(cfg.api_key)
    else:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider = OpenAIProvider(base_url=cfg.endpoint, api_key=cfg.api_key)
        provider.client.max_retries = 0  # Retrying is the only retry layer
        make = lambda name: OpenAIChatModel(name, provider=provider)  # noqa: E731
    return build_chain(cfg.models, make)


class JudgeVerdict(BaseModel):
    grounded: bool
    answers_question: bool
    honest_caveats: bool
    no_invented_numbers: bool
    reason: str = ""

    @property
    def score(self) -> float:
        return sum([self.grounded, self.answers_question, self.honest_caveats, self.no_invented_numbers]) / 4

    @property
    def passed(self) -> bool:
        """An answer to the wrong question (or a refusal) never passes, however grounded it is."""
        return self.answers_question and self.score >= JUDGE_PASS_MIN


_JUDGE_INSTRUCTIONS = """You grade an analytics assistant's answer against the query result it was based on.
Return a verdict with four booleans:
- grounded: every claim is supported by the data rows (or the reference rows, when given).
- answers_question: it addresses the user's question directly.
- honest_caveats: it does not overstate certainty; assumptions or limits are stated when relevant.
- no_invented_numbers: every number in the answer appears in the data or reference rows (rounding and $K/$M formatting are fine).
Also apply the case-specific rubric. Be strict: a wrong figure or a wrong winner fails grounded and no_invented_numbers.
When there are no data rows and the question asks for a definition or a general explanation, illustrative example numbers are fine
and the answer counts as grounded if the explanation is correct; only claims about this account's actual performance need data rows."""


def judge_answer(
    model: Model,
    question: str,
    rubric: str,
    data_rows: list[dict],
    answer_md: str,
    *,
    reference_rows: list[dict] | None = None,
    glossary: str = "",
    audit: AuditSink | None = None,
    context: RunContextInfo | None = None,
    pack_name: str = "ads",
    agent_models: Sequence[str] = (),
) -> JudgeVerdict:
    agent: Agent[None, JudgeVerdict] = Agent(model, output_type=JudgeVerdict, instructions=_JUDGE_INSTRUCTIONS, retries=1, name="adpilot-judge", capabilities=[NoNullSchemas()])
    prompt = f"Question: {question}\n\nRubric: {rubric}\n\nData rows the answer was based on: {data_rows}\n\n"
    if reference_rows:
        prompt += f"Reference rows, what a correct query returns (max 30): {reference_rows[:30]}\n\n"
    if glossary:
        prompt += f"Domain facts the assistant is instructed to use (a claim that restates them is grounded):\n{glossary}\n\n"
    prompt += f"Answer to grade:\n{answer_md}"
    started, t0 = datetime.now(UTC), time.perf_counter()
    messages, usage, caveats = [], None, []
    def void(reason: str) -> JudgeVerdict:
        return JudgeVerdict(grounded=False, answers_question=False, honest_caveats=False, no_invented_numbers=False, reason=reason[:200])

    try:
        result = agent.run_sync(prompt)
        verdict = result.output
        messages, usage = result.new_messages(), (result.usage() if callable(result.usage) else result.usage)
        used = summarize_messages(messages).model_used
        if any(same_model(a, used) for a in agent_models):
            verdict = void(f"judge_error: self-judged by {used}")  # excluded like an outage, never counted
    except Exception as exc:  # noqa: BLE001 — a judge outage must not crash the run
        verdict = void(f"judge_error: {exc}")
    if verdict.reason.startswith("judge_error"):
        caveats = [f"JudgeError: {verdict.reason}"]
    if audit is not None:
        ctx = (context or RunContextInfo()).model_copy(update={"source": "judge"})
        audit.record(build_record(
            trace_id=new_trace_id(), ts=started, latency_s=time.perf_counter() - t0, question=question, answer_md=verdict.model_dump_json(),
            sql=None, refused=False, confidence=verdict.score, caveats=caveats, messages=messages, usage=usage,
            model_requested=primary_model_name(model), context=ctx, pack_name=pack_name, prompt_hash=None,
            extra_attributes={"rubric": rubric},
        ))
    return verdict


@dataclass
class CalibratedJudge(Evaluator[Any, Trace, dict]):
    model: Model | None
    connector: Any
    pack: Any
    audit: Any = None
    run_id: str | None = None
    agent_models: Sequence[str] = ()

    def evaluate(self, ctx: EvaluatorContext) -> dict:
        if self.model is None:
            return {}
        family = (ctx.metadata or {}).get("family", "")
        exp: Expected = ctx.expected_output
        trace: Trace = ctx.output
        question = ctx.inputs.final_question
        if family == "narrative":
            # Grade against the rows the agent actually queried; the reference query only shows what a correct answer needs.
            ref = reference_rows(self.connector, self.pack, exp) if exp.sql else []
            v = judge_answer(
                self.model, question, exp.rubric or "", trace.rows_seen or trace.answer.data or [], trace.answer.answer_md,
                reference_rows=ref, glossary=self.pack.glossary, audit=self.audit, context=RunContextInfo(source="judge", run_id=self.run_id, case_name=ctx.name, family=family), pack_name=self.pack.name,
                agent_models=self.agent_models,
            )
            out: dict = {"judge": v.score, "judge_pass": v.passed}
            if v.reason.startswith("judge_error"):
                out["judge_error"] = True
            return out
        if family in ACCURACY_FAMILIES:
            if Factual(self.connector, self.pack).evaluate(ctx)["factual"].value:
                return {}
            rows = reference_rows(self.connector, self.pack, exp)
            rubric = "The answer states the same figures (and the same winner/ranking, if any) as the reference rows."
            v = judge_answer(
                self.model, question, rubric, rows, trace.answer.answer_md,
                audit=self.audit, context=RunContextInfo(source="judge", run_id=self.run_id, case_name=ctx.name, family=family), pack_name=self.pack.name,
                agent_models=self.agent_models,
            )
            out = {"judge_rescued": v.grounded and v.no_invented_numbers and v.answers_question}
            if v.reason.startswith("judge_error"):
                out["judge_error"] = True
            return out
        return {}

    async def evaluate_async(self, ctx: EvaluatorContext) -> dict:
        # pydantic-evals calls evaluate() synchronously from the running event-loop thread; judge_answer()
        # uses Agent.run_sync(), which raises if called on that thread. Run it on a worker thread instead.
        return await anyio.to_thread.run_sync(self.evaluate, ctx)


@dataclass
class CalibrationResult:
    n: int
    agreement: float
    mismatches: list[str]

    @property
    def reliable(self) -> bool:
        return self.n > 0 and self.agreement >= JUDGE_RELIABLE_MIN


def run_calibration(
    model: Model, pack: Any, audit: AuditSink | None = None, run_id: str | None = None, agent_models: Sequence[str] = ()
) -> CalibrationResult:
    entries = yaml.safe_load((pack.root / "judge_calibration.yaml").read_text())["entries"]
    mismatches: list[str] = []
    for e in entries:
        v = judge_answer(
            model, e["question"], "Answer must be correct, grounded and honest.", e["data"], e["answer"], glossary=pack.glossary,
            audit=audit, context=RunContextInfo(source="judge", run_id=run_id, case_name=e["name"], family="calibration"), pack_name=pack.name,
            agent_models=agent_models,
        )
        if v.passed != (e["verdict"] == "pass"):
            mismatches.append(e["name"])
    n = len(entries)
    return CalibrationResult(n=n, agreement=(n - len(mismatches)) / n if n else 0.0, mismatches=mismatches)
