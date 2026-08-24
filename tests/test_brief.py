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
    assert "## Answers" not in brief.markdown and "## Unavailable" in brief.markdown  # nothing was answered


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


def test_brief_says_so_what_and_now_what(agent, deps):
    model, prompts = brief_model()
    brief = run_brief(deps, agent, model)
    assert brief.summary is not None and brief.caveats == []  # "7 days" in the action comes from the question text
    md = brief.markdown
    assert md.startswith("# TikTok carries the most spend")
    assert "## What changed" in md and "it is the largest line in the budget" in md
    assert "## What to do" in md and "Review TikTok bids" in md
    assert f"trace `{brief.answers[0][2]}`" in md
    assert len(prompts) == 1


def test_synthesis_is_audited_with_the_brief(agent, deps):
    model, _ = brief_model()
    brief = run_brief(deps, agent, model)
    rows = [c for c in deps.audit.calls if c.source == "brief"]
    assert [r.case_name for r in rows] == ["q1", "q2", "q3", "q4", "synthesis"]
    assert rows[-1].run_id == brief.run_id and rows[-1].error_kind is None


def test_unavailable_answers_never_reach_synthesis(agent, deps):
    model, prompts = brief_model(refuse="14-day forecast", fail="budget optimizer")
    brief = run_brief(deps, agent, model)
    assert "14-day forecast" not in prompts[0] and "budget optimizer" not in prompts[0]
    assert "## Unavailable" in brief.markdown


def test_nothing_usable_means_no_synthesis_call(agent, deps):
    model, prompts = brief_model(refuse="the")  # every pack question contains "the"
    brief = run_brief(deps, agent, model)
    assert prompts == [] and brief.summary is None and len(brief.unavailable) == 4


def test_no_model_writes_no_synthesis_row(agent, deps):
    run_brief(deps, agent, None)
    assert "synthesis" not in [c.case_name for c in deps.audit.calls]


def test_failed_synthesis_degrades_to_raw_answers(agent, deps):
    model, _ = brief_model(synth=ModelHTTPError(503, "primary", body={"error": "down"}))
    brief = run_brief(deps, agent, model)
    assert brief.summary is None
    assert brief.caveats == ["BriefSynthesisFailed: ModelUnavailable"]
    assert "## Answers" in brief.markdown and "> ⚠ BriefSynthesisFailed" in brief.markdown
    last = deps.audit.calls[-1]
    assert last.case_name == "synthesis" and last.error_kind == "ModelUnavailable"


def test_citing_an_unavailable_question_is_retried_then_fails(agent, deps):
    bad = {**SUMMARY, "findings": [{**SUMMARY["findings"][0], "source": 3}]}  # Q3 refuses below
    model, prompts = brief_model(synth=bad, refuse="14-day forecast")
    brief = run_brief(deps, agent, model)
    assert len(prompts) == 2  # one retry, then give up
    assert brief.summary is None and brief.caveats[0].startswith("BriefSynthesisFailed")


def test_a_number_not_in_the_answers_is_flagged(agent, deps):
    findings = [
        {"what": "TikTok spent about 74,267", "so_what": "largest line", "source": 1},  # rounded: grounded
        {"what": "Google spent 99999", "so_what": "invented", "source": 2},
    ]
    model, _ = brief_model(synth={**SUMMARY, "findings": findings})
    brief = run_brief(deps, agent, model)
    assert brief.caveats == ["BriefUngrounded: 99999"]
    assert "> ⚠ BriefUngrounded: 99999" in brief.markdown


def test_no_actions_says_so(agent, deps):
    model, _ = brief_model(synth={**SUMMARY, "actions": []})
    assert "- No action this time." in run_brief(deps, agent, model).markdown


def test_synthesis_text_is_redacted_before_it_is_published(agent, deps):
    """The brief goes to a public issue. ask() redacts every answer_md; the synthesis must not be the hole."""
    leaky = {**SUMMARY, "watch": ["ping me@example.com with OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnopqrstuv"]}
    model, _ = brief_model(synth=leaky)
    brief = run_brief(deps, agent, model)
    assert "me@example.com" not in brief.markdown and "sk-or-v1-abc" not in brief.markdown
    assert "[redacted:email]" in brief.markdown and "> ⚠ OutputPolicy" in brief.markdown
    assert "OutputPolicy" in brief.caveats
    assert "me@example.com" not in deps.audit.calls[-1].answer_md


def test_raw_view_never_shows_a_fallback_answer_as_the_answer(agent, deps):
    """With no model every question gets a canned all-time table that does not answer it; show why, not the table."""
    brief = run_brief(deps, agent, None)
    assert "pre-defined query" not in brief.markdown and "```text" not in brief.markdown
    for q in questions(deps.pack):
        assert f"- {q} — ModelUnavailable" in brief.markdown
