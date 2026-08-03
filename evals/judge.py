"""LLM judge: separate model chain from the agent, rubric-based verdicts, and a calibration set that checks the judge."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio
import yaml
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from adpilot.core.audit import AuditSink, RunContextInfo, build_record, new_trace_id, primary_model_name
from adpilot.core.models import RateLimited
from evals.cases import ACCURACY_FAMILIES, Expected, reference_rows
from evals.evaluators import Factual
from evals.task import Trace

DEFAULT_JUDGE_PRIMARY = "google/gemma-4-26b-a4b-it:free"
DEFAULT_JUDGE_FALLBACK = "inclusionai/ling-3.0-flash-vl:free"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1"
JUDGE_RELIABLE_MIN = 0.8
JUDGE_PASS_MIN = 0.75


@dataclass
class JudgeConfig:
    endpoint: str
    api_key: str | None
    primary: str
    fallback: str | None


def judge_config(env: Mapping[str, str] | None = None) -> JudgeConfig:
    env = os.environ if env is None else env
    endpoint = env.get("LLM_JUDGE_ENDPOINT_URL") or env.get("LLM_ENDPOINT_URL") or DEFAULT_ENDPOINT
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/chat/completions"):
        endpoint = endpoint[: -len("/chat/completions")]
    fallback = env.get("LLM_JUDGE_FALLBACK_MODEL")
    return JudgeConfig(
        endpoint=endpoint,
        api_key=env.get("LLM_JUDGE_BEARER_TOKEN") or env.get("OPENROUTER_API_KEY") or env.get("LLM_BEARER_TOKEN") or None,
        primary=env.get("LLM_JUDGE_TARGET_MODEL") or DEFAULT_JUDGE_PRIMARY,
        fallback=(DEFAULT_JUDGE_FALLBACK if fallback is None else (fallback or None)),
    )


def build_judge_model(cfg: JudgeConfig) -> Model | None:
    if not cfg.api_key:
        return None
    if "openrouter.ai" in cfg.endpoint:
        from pydantic_ai.models.openrouter import OpenRouterModel
        from pydantic_ai.providers.openrouter import OpenRouterProvider

        provider = OpenRouterProvider(api_key=cfg.api_key)
        make = lambda name: OpenRouterModel(name, provider=provider)  # noqa: E731
    else:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider = OpenAIProvider(base_url=cfg.endpoint, api_key=cfg.api_key)
        make = lambda name: OpenAIChatModel(name, provider=provider)  # noqa: E731
    chain = [RateLimited(make(cfg.primary))]
    if cfg.fallback:
        chain.append(RateLimited(make(cfg.fallback)))
    return FallbackModel(*chain) if len(chain) > 1 else chain[0]


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
) -> JudgeVerdict:
    agent: Agent[None, JudgeVerdict] = Agent(model, output_type=JudgeVerdict, instructions=_JUDGE_INSTRUCTIONS, retries=1, name="adpilot-judge")
    prompt = f"Question: {question}\n\nRubric: {rubric}\n\nData rows the answer was based on: {data_rows}\n\n"
    if reference_rows:
        prompt += f"Reference rows, what a correct query returns (max 30): {reference_rows[:30]}\n\n"
    if glossary:
        prompt += f"Domain facts the assistant is instructed to use (a claim that restates them is grounded):\n{glossary}\n\n"
    prompt += f"Answer to grade:\n{answer_md}"
    started, t0 = datetime.now(UTC), time.perf_counter()
    messages, usage, caveats = [], None, []
    try:
        result = agent.run_sync(prompt)
        verdict = result.output
        messages, usage = result.new_messages(), (result.usage() if callable(result.usage) else result.usage)
    except Exception as exc:  # noqa: BLE001 — a judge outage must not crash the run
        verdict = JudgeVerdict(grounded=False, answers_question=False, honest_caveats=False, no_invented_numbers=False, reason=f"judge_error: {exc}"[:200])
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


def run_calibration(model: Model, pack: Any, audit: AuditSink | None = None, run_id: str | None = None) -> CalibrationResult:
    entries = yaml.safe_load((pack.root / "judge_calibration.yaml").read_text())["entries"]
    mismatches: list[str] = []
    for e in entries:
        v = judge_answer(
            model, e["question"], "Answer must be correct, grounded and honest.", e["data"], e["answer"], glossary=pack.glossary,
            audit=audit, context=RunContextInfo(source="judge", run_id=run_id, case_name=e["name"], family="calibration"), pack_name=pack.name,
        )
        if v.passed != (e["verdict"] == "pass"):
            mismatches.append(e["name"])
    n = len(entries)
    return CalibrationResult(n=n, agreement=(n - len(mismatches)) / n if n else 0.0, mismatches=mismatches)
