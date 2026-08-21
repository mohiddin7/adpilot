"""The daily brief. The one agent answers the pack's briefing questions through ask(); the answers that
survive are rendered as the brief. No second agent: guardrails, fallbacks, redaction and the audit record
all come from ask()."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from adpilot.core.agent import AnalystAnswer, Refusal, ask, fell_back, refused
from adpilot.core.audit import RunContextInfo, new_trace_id
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import Pack

RAW_ROWS = 10  # rows per answer in the raw view; keeps an issue body far below GitHub's 65 536 chars


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
    caveats: list[str]
    markdown: str


def questions(pack: Pack) -> list[str]:
    return list((pack.raw.get("briefing") or {}).get("questions") or [])


def run_brief(deps: AgentDeps, agent: Agent[AgentDeps, AnalystAnswer | Refusal], model: Model | None) -> Brief:
    """Every pack question through ask() under one run_id (source="brief", case_name q1…qN)."""
    run_id = new_trace_id()
    answers = []
    for i, q in enumerate(questions(deps.pack), 1):
        deps.run_context = RunContextInfo(source="brief", run_id=run_id, case_name=f"q{i}")
        answer, _, trace_id = ask(agent, deps, q, model=model)
        answers.append((q, answer, trace_id))
    unavailable = [q for q, a, _ in answers if refused(a) or fell_back(a)]
    md = _render(deps.pack, answers, unavailable, run_id)
    return Brief(run_id, answers, unavailable, None, [], md)


def _render(pack: Pack, answers, unavailable: list[str], run_id: str) -> str:
    out = ["# Daily brief — raw answers", "", "## Answers"]
    for i, (q, a, _) in enumerate(answers, 1):
        out += ["", f"### Q{i}. {q}", "", a.answer_md]
        if a.data:
            out += ["", "```text", pd.DataFrame(a.data[:RAW_ROWS]).to_string(index=False), "```"]
    if unavailable:
        out += ["", "## Unavailable", *(f"- {q}" for q in unavailable)]
    out += ["", "## Questions", *(f"{i}. {q} — trace `{t}`" for i, (q, _, t) in enumerate(answers, 1))]
    out += ["", "---", f"pack `{pack.name}` · prompt `{pack.prompt_hash}` · brief run `{run_id}` "
            f"(`adpilot audit export --run {run_id}`)", ""]
    return "\n".join(out)
