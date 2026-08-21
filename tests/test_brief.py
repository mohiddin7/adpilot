"""Brief tests. One FunctionModel scripts both the per-question agent runs and the tool-less synthesis call
(recognised by having no function tools); DuckDB is the data source, MemorySink the audit trail."""

import dataclasses

import pytest
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import FunctionModel

from adpilot.core.agent import build_agent
from adpilot.core.brief import questions, run_brief

GOLD = "fct_unified_marketing_performance"
GOOD_SQL = f"SELECT platform, ROUND(SUM(spend), 2) AS spend FROM {GOLD} GROUP BY platform ORDER BY spend DESC"
SUMMARY = {
    "headline": "TikTok carries the most spend",
    "findings": [{"what": "TikTok spent 74266.7", "so_what": "it is the largest line in the budget", "source": 1}],
    "actions": [{"do": "Review TikTok bids", "why": "largest spend", "expect": "lower CPA within 7 days"}],
    "watch": ["Google conversions"],
}


def first_prompt(messages) -> str:
    return next(p.content for p in messages[0].parts if p.part_kind == "user-prompt")


def brief_model(synth=SUMMARY, refuse="", fail="", data=None):
    """Returns (model, synth_prompts).

    Each briefing question gets one run_sql then a final answer. A question containing `refuse` refuses; one
    containing `fail` raises a 503, which sends ask() down its rule-based path. `synth` is the synthesis output
    dict, or an exception for the synthesis call to raise. `data` overrides the answer's rows.
    """
    synth_prompts: list[str] = []

    def fn(messages, info):
        q = first_prompt(messages)
        if not info.function_tools:
            synth_prompts.append(q)
            if isinstance(synth, Exception):
                raise synth
            return ModelResponse(parts=[ToolCallPart("final_result", synth)])
        if fail and fail in q:
            raise ModelHTTPError(503, "primary", body={"error": "down"})
        if refuse and refuse in q:
            return ModelResponse(parts=[ToolCallPart("final_result_Refusal", {"reason": "not answerable here"})])
        if any(p.part_kind == "tool-return" for p in messages[-1].parts):
            final = {"answer_md": "TikTok leads spend at 74266.7.", "sql": GOOD_SQL}
            if data is not None:
                final["data"] = data
            return ModelResponse(parts=[ToolCallPart("final_result_AnalystAnswer", final)])
        return ModelResponse(parts=[ToolCallPart("run_sql", {"sql": GOOD_SQL})])

    return FunctionModel(fn), synth_prompts


@pytest.fixture
def agent():
    return build_agent()


def with_questions(deps, qs):
    deps.pack = dataclasses.replace(deps.pack, raw={**deps.pack.raw, "briefing": {"questions": qs}})
    return deps


def test_questions_come_from_the_pack(pack):
    assert len(questions(pack)) == 4
    assert questions(dataclasses.replace(pack, raw={k: v for k, v in pack.raw.items() if k != "briefing"})) == []


def test_no_model_lists_every_fallback_as_unavailable(agent, deps):
    brief = run_brief(deps, agent, None)
    assert brief.summary is None
    assert brief.unavailable == questions(deps.pack)
    assert "## Answers" in brief.markdown and "## Unavailable" in brief.markdown


def test_refusal_and_canned_fallback_are_both_unavailable(agent, deps):
    model, _ = brief_model(refuse="14-day forecast", fail="budget optimizer")
    brief = run_brief(deps, agent, model)
    qs = questions(deps.pack)
    assert brief.unavailable == [qs[2], qs[3]]
    assert brief.answers[3][1].confidence == 0.3  # the canned query matched — still not a finding


def test_every_question_is_audited_under_one_run_id(agent, deps):
    model, _ = brief_model()
    brief = run_brief(deps, agent, model)
    rows = [c for c in deps.audit.calls if c.source == "brief"]
    assert [r.case_name for r in rows][:4] == ["q1", "q2", "q3", "q4"]
    assert {r.run_id for r in rows} == {brief.run_id}
    assert [t for _, _, t in brief.answers] == [r.trace_id for r in rows][:4]


def test_raw_answers_show_at_most_ten_rows(agent, deps):
    model, _ = brief_model(synth=RuntimeError("no synthesis"), data=[{"n": 7000000 + i} for i in range(50)])
    brief = run_brief(with_questions(deps, questions(deps.pack)[:1]), agent, model)
    assert "7000009" in brief.markdown and "7000010" not in brief.markdown
