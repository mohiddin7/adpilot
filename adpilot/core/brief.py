"""The daily brief. The one agent answers the pack's briefing questions through ask(); one tool-less call then
turns the surviving answers into what changed (so what) and what to do (now what).

No second agent: guardrails, fallbacks and the audit record come from ask(). The synthesis call has no tools, so
it cannot fetch a number the answers do not contain — it can only mis-state one, which _ungrounded() flags. The
brief is published to a public issue, so the rendered markdown goes through the same layer-5 redaction ask()
applies to every answer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models import Model

from adpilot.core.agent import FALLBACK_NOTE, AnalystAnswer, Refusal, _classify, ask, fell_back, refused
from adpilot.core.audit import (
    RunContextInfo,
    build_record,
    new_trace_id,
    primary_model_name,
    summarize_messages,
)
from adpilot.core.guardrails import redact_output
from adpilot.core.models import NoNullSchemas
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import Pack

RAW_ROWS = 10  # rows per answer in the raw view; keeps an issue body far below GitHub's 65 536 chars
# Not a digit glued to a letter ("Q3" is a citation), and a comma only as a thousands separator ("3," is punctuation).
_NUM = re.compile(r"(?<![A-Za-z_\d])\d+(?:,\d{3})*(?:\.\d+)?")

SYNTH_INSTRUCTIONS = """\
You turn analyst answers into a short daily brief for the person who owns the ad budget.
- Use only numbers that appear in the supplied answers or rows. Never compute, extrapolate or invent a figure.
- Each finding states what changed and why it matters (so_what), and sets `source` to the number of the
  question it came from.
- Pair every finding with an action (imperative, with the effect to expect and by when) or a `watch` entry.
- 2 to 5 findings, at most 3 actions. No action is better than an invented one."""


class Finding(BaseModel):
    what: str = Field(description="the observation")
    so_what: str = Field(description="why it matters, quantified from the supplied answers")
    source: int = Field(description="number of the question this finding comes from")


class Action(BaseModel):
    do: str = Field(description="imperative: the move to make")
    why: str = Field(description="the finding it answers")
    expect: str = Field(description="the effect to look for, and by when")


class BriefSummary(BaseModel):
    headline: str
    findings: list[Finding] = Field(max_length=5)
    actions: list[Action] = Field(max_length=3)
    watch: list[str] = []


@dataclass
class Brief:
    run_id: str
    answers: list[tuple[str, AnalystAnswer, str]]  # question, answer, trace_id
    unavailable: list[str]  # questions that refused or fell back; never shown as findings
    summary: BriefSummary | None  # None: no model, nothing survived, or synthesis failed
    caveats: list[str]  # BriefSynthesisFailed, BriefUngrounded
    markdown: str


def questions(pack: Pack) -> list[str]:
    return list((pack.raw.get("briefing") or {}).get("questions") or [])


def run_brief(deps: AgentDeps, agent: Agent[AgentDeps, AnalystAnswer | Refusal], model: Model | None) -> Brief:
    """Every pack question through ask() under one run_id (source="brief", case_name q1…qN), then one
    synthesis over the answers that neither refused nor fell back."""
    run_id = new_trace_id()
    answers = []
    for i, q in enumerate(questions(deps.pack), 1):
        deps.run_context = RunContextInfo(source="brief", run_id=run_id, case_name=f"q{i}")
        answer, _, trace_id = ask(agent, deps, q, model=model)
        answers.append((q, answer, trace_id))
    usable = {i: a for i, a in enumerate(answers, 1) if not (refused(a[1]) or fell_back(a[1]))}
    unavailable = [q for i, (q, _, _) in enumerate(answers, 1) if i not in usable]
    summary, caveats, model_used = None, [], None
    synth_model = model or agent.model
    if synth_model is not None and usable:
        summary, caveats, model_used = _synthesize(deps, synth_model, usable, run_id)
    if summary is not None:
        caveats += _ungrounded(summary, _source_text(usable))
    md, leaked = redact_output(_render(deps.pack, answers, unavailable, summary, caveats, run_id, model_used))
    if leaked:
        caveats.append("OutputPolicy")
        md, _ = redact_output(_render(deps.pack, answers, unavailable, summary, caveats, run_id, model_used))
    return Brief(run_id, answers, unavailable, summary, caveats, md)


def _synthesize(
    deps: AgentDeps, model: Model | str, usable: dict, run_id: str
) -> tuple[BriefSummary | None, list[str], str | None]:
    """One tool-less call, audited like the judge's. Any failure returns no summary and a caveat, never raises."""
    synth: Agent[None, BriefSummary] = Agent(
        model, output_type=BriefSummary, instructions=SYNTH_INSTRUCTIONS, retries=1, name="adpilot-brief",
        capabilities=[NoNullSchemas()],
    )

    @synth.output_validator
    def cites_a_usable_question(out: BriefSummary) -> BriefSummary:
        bad = sorted({f.source for f in out.findings} - usable.keys())
        if bad:
            raise ModelRetry(f"`source` must be one of {sorted(usable)}; got {bad}")
        return out

    prompt = "\n\n".join(
        f"## Question {i}: {q}\nAnswer: {a.answer_md}\nSQL: {a.sql}\nRows: {json.dumps(a.data, default=str)}"
        for i, (q, a, _) in usable.items()
    )
    started, t0 = datetime.now(UTC), time.perf_counter()
    summary, messages, usage, kind, detail = None, [], None, None, ""
    try:
        result = synth.run_sync(prompt)
        summary, messages = result.output, result.new_messages()
        usage = result.usage() if callable(result.usage) else result.usage
    except Exception as exc:  # noqa: BLE001 — a failed synthesis degrades to raw answers; it never loses the brief
        kind, detail = _classify(exc)
    deps.audit.record(build_record(
        trace_id=new_trace_id(), ts=started, latency_s=time.perf_counter() - t0,
        question="brief synthesis over " + ", ".join(f"q{i}" for i in usable),
        answer_md=redact_output(summary.model_dump_json())[0] if summary else "", sql=None, refused=False,
        confidence=1.0 if summary else 0.0,
        caveats=[f"{kind}: brief synthesis failed ({detail[:160]})"] if kind else [],
        messages=messages, usage=usage, model_requested=primary_model_name(model),
        context=RunContextInfo(source="brief", run_id=run_id, case_name="synthesis"),
        pack_name=deps.pack.name, prompt_hash=deps.pack.prompt_hash,
    ))
    return summary, ([f"BriefSynthesisFailed: {kind}"] if kind else []), summarize_messages(messages).model_used


def _source_text(usable: dict) -> str:
    return "\n".join(f"{q}\n{a.answer_md}\n{a.sql}\n{json.dumps(a.data, default=str)}" for q, a, _ in usable.values())


def _ungrounded(summary: BriefSummary, source_text: str) -> list[str]:
    """Numbers in the brief that match no number in the answers, rows or questions at the brief's own precision."""
    # ponytail: numeric-token match; "$1.2k" for 1234 or "23%" for 0.23 warns falsely — unit-aware parsing if noisy
    have = [float(n.replace(",", "")) for n in _NUM.findall(source_text)]
    claims = " ".join(
        [summary.headline, *summary.watch]
        + [f"{f.what} {f.so_what}" for f in summary.findings]
        + [f"{a.do} {a.why} {a.expect}" for a in summary.actions]
    )
    missing = []
    for tok in dict.fromkeys(_NUM.findall(claims)):
        value = float(tok.replace(",", ""))
        places = len(tok.split(".")[1]) if "." in tok else 0
        if not any(round(h, places) == value for h in have):
            missing.append(tok)
    return [f"BriefUngrounded: {', '.join(missing)}"] if missing else []


def _why_unavailable(answer: AnalystAnswer) -> str:
    if refused(answer):
        return "refused"
    return next(c.split(":")[0] for c in answer.caveats if FALLBACK_NOTE in c)


def _render(pack: Pack, answers, unavailable, summary: BriefSummary | None, caveats, run_id: str, model_used) -> str:
    out = [f"# {summary.headline if summary else 'Daily brief — raw answers'}", ""]
    out += [f"> ⚠ {c}" for c in caveats]
    if summary:
        out += ["", "## What changed"]
        out += [
            f"- **{f.what}** — {f.so_what} _(Q{f.source}, trace `{answers[f.source - 1][2]}`)_"
            for f in summary.findings
        ]
        out += ["", "## What to do"]
        out += [f"- **{a.do}** — {a.why}. Expect: {a.expect}" for a in summary.actions] or ["- No action this time."]
        if summary.watch:
            out += ["", "## Watching", *(f"- {w}" for w in summary.watch)]
    else:
        # A fallback's canned table does not answer the question it sits under; it is listed below, with why.
        shown = [(i, q, a) for i, (q, a, _) in enumerate(answers, 1) if q not in unavailable]
        if shown:
            out += ["", "## Answers"]
        for i, q, a in shown:
            out += ["", f"### Q{i}. {q}", "", a.answer_md]
            if a.data:
                out += ["", "```text", pd.DataFrame(a.data[:RAW_ROWS]).to_string(index=False), "```"]
    if unavailable:
        out += ["", "## Unavailable", *(f"- {q} — {_why_unavailable(a)}" for q, a, _ in answers if q in unavailable)]
    out += ["", "## Questions", *(f"{i}. {q} — trace `{t}`" for i, (q, _, t) in enumerate(answers, 1))]
    out += ["", "---", f"pack `{pack.name}` · prompt `{pack.prompt_hash}` · synthesis model `{model_used or 'none'}` · "
            f"brief run `{run_id}` (`adpilot audit export --run {run_id}`)", ""]
    return "\n".join(out)
