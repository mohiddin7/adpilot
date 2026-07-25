from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_evals.evaluators import EvaluatorContext

from adpilot.core.agent import AnalystAnswer
from evals.cases import Expected
from evals.judge import (
    CalibratedJudge,
    JudgeVerdict,
    build_judge_model,
    judge_answer,
    judge_config,
    run_calibration,
)
from evals.task import EvalInputs, Trace


def verdict_model(**flags):
    def fn(messages, info):
        args = {"grounded": True, "answers_question": True, "honest_caveats": True, "no_invented_numbers": True, "reason": "fine"}
        args.update(flags)
        return ModelResponse(parts=[ToolCallPart("final_result", args)])

    return FunctionModel(fn)


def test_config_defaults_and_fallbacks():
    cfg = judge_config({})
    assert cfg.api_key is None and cfg.endpoint == "https://openrouter.ai/api/v1"
    assert cfg.primary == "google/gemma-4-26b-a4b-it:free" and cfg.fallback == "inclusionai/ling-3.0-flash-vl:free"
    cfg = judge_config({"LLM_BEARER_TOKEN": "agent-key", "LLM_ENDPOINT_URL": "https://x.example/v1/chat/completions", "LLM_TARGET_MODEL": "agent-model"})
    assert cfg.api_key == "agent-key" and cfg.endpoint == "https://x.example/v1" and cfg.primary != "agent-model"
    cfg = judge_config({"LLM_JUDGE_BEARER_TOKEN": "j", "LLM_JUDGE_TARGET_MODEL": "m", "LLM_JUDGE_FALLBACK_MODEL": ""})
    assert cfg.api_key == "j" and cfg.primary == "m" and cfg.fallback is None


def test_build_model_none_without_key():
    assert build_judge_model(judge_config({})) is None
    m = build_judge_model(judge_config({"LLM_JUDGE_BEARER_TOKEN": "k"}))
    assert m is not None and "gemma" in m.model_name


def test_judge_answer_scores():
    v = judge_answer(verdict_model(grounded=False), "q", "rubric", [{"spend": 1}], "answer")
    assert isinstance(v, JudgeVerdict) and v.score == 0.75 and v.reason == "fine"


def test_judge_answer_error_is_zero():
    def boom(messages, info):
        raise RuntimeError("down")

    v = judge_answer(FunctionModel(boom), "q", "r", [], "a")
    assert v.score == 0.0 and v.reason.startswith("judge_error")


def ctx(trace, expected, family):
    return EvaluatorContext(name="c", inputs=EvalInputs(name="c", question="q"), metadata={"family": family, "tool": None, "group": None, "consistency": False},
                            expected_output=expected, output=trace, duration=0.0, _span_tree=None, attributes={}, metrics={})


def test_calibrated_judge_narrative_and_rescue(eval_duck, pack):
    j = CalibratedJudge(verdict_model(), eval_duck, pack)
    narrative = ctx(Trace(answer=AnalystAnswer(answer_md="TikTok spent more.")), Expected(sql="SELECT 1 AS x FROM {gold} LIMIT 1", rubric="r"), "narrative")
    assert j.evaluate(narrative) == {"judge": 1.0, "judge_pass": True}
    failed = ctx(Trace(answer=AnalystAnswer(answer_md="no idea")), Expected(sql="SELECT ROUND(SUM(spend), 2) AS spend FROM {gold}", value=130244.9, column="spend"), "factual")
    assert j.evaluate(failed) == {"judge_rescued": True}
    passed = ctx(Trace(answer=AnalystAnswer(answer_md="$130,244.90")), Expected(sql="SELECT ROUND(SUM(spend), 2) AS spend FROM {gold}", value=130244.9, column="spend"), "factual")
    assert j.evaluate(passed) == {}
    assert CalibratedJudge(None, eval_duck, pack).evaluate(narrative) == {}


def test_calibration_agreement(pack):
    always_pass = verdict_model()
    res = run_calibration(always_pass, pack)
    assert res.n == 10 and res.agreement == 0.5 and len(res.mismatches) == 5  # 5 fail-labelled entries mismatched


def test_judge_answer_records_a_judge_call(pack):
    from adpilot.core.audit import MemorySink, RunContextInfo

    sink = MemorySink()
    v = judge_answer(verdict_model(), "q", "rubric", [{"spend": 1}], "answer", audit=sink, context=RunContextInfo(source="judge", run_id="r", case_name="c"))
    rec = sink.calls[-1]
    assert v.score == 1.0 and rec.source == "judge" and rec.run_id == "r" and rec.case_name == "c" and rec.requests == 1
    assert rec.question == "q" and '"grounded":true' in rec.answer_md and rec.attributes["rubric"] == "rubric"


def test_judge_error_is_recorded_as_judge_error_kind():
    from adpilot.core.audit import MemorySink

    def boom(messages, info):
        raise RuntimeError("down")

    sink = MemorySink()
    judge_answer(FunctionModel(boom), "q", "r", [], "a", audit=sink)
    assert sink.calls[-1].error_kind == "JudgeError" and sink.calls[-1].caveats[0].startswith("JudgeError: judge_error")


def test_narrative_judge_sees_the_agents_own_rows_and_the_reference_rows(eval_duck, pack):
    """The answer is graded against the rows it was based on; the reference query is a second block, not a substitute."""
    seen = {}

    def fn(messages, info):
        seen["prompt"] = messages[-1].parts[-1].content
        return ModelResponse(parts=[ToolCallPart("final_result", {"grounded": True, "answers_question": True, "honest_caveats": True, "no_invented_numbers": True})])

    j = CalibratedJudge(FunctionModel(fn), eval_duck, pack)
    trace = Trace(answer=AnalystAnswer(answer_md="7.22M impressions", data=[{"impressions": 7220000}]))
    j.evaluate(ctx(trace, Expected(sql="SELECT 1 AS x FROM {gold} LIMIT 1", rubric="r"), "narrative"))
    assert "'impressions': 7220000" in seen["prompt"].split("Reference rows")[0]
    assert "'x': 1" in seen["prompt"].split("Reference rows")[1]
