import os
from pathlib import Path

import yaml
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import evals.run as run_module
from evals.cases import load_cases
from evals.deterministic import SYNTHETIC, model_for
from evals.run import run

# Answers that the judge-calibration set (packs/ads/judge_calibration.yaml) labels "fail". A judge that flags
# exactly these and passes everything else agrees 100% with the calibration labels, so the judge is reported
# reliable and its quality score can be exercised end to end. The judge prompt quotes the answer verbatim.
_CALIBRATION_FAIL_MARKERS = tuple(
    e["answer"] for e in yaml.safe_load(Path("packs/ads/judge_calibration.yaml").read_text())["entries"] if e["verdict"] == "fail"
)


def _scripted_agent_model() -> FunctionModel:
    """Always finalizes with a fixed AnalystAnswer — no tool calls, no network."""

    def fn(messages, info):
        return ModelResponse(parts=[ToolCallPart("final_result_AnalystAnswer", {"answer_md": "scripted", "sql": None})])

    return FunctionModel(fn)


def _scripted_judge_model() -> FunctionModel:
    """Fails only the answers the calibration set labels 'fail'; passes everything else, including
    any narrative answer this test produces ('scripted' matches none of the fail markers)."""

    def fn(messages, info):
        prompt = messages[-1].parts[0].content
        bad = any(marker in prompt for marker in _CALIBRATION_FAIL_MARKERS)
        flags = {"grounded": not bad, "answers_question": not bad, "honest_caveats": not bad, "no_invented_numbers": not bad, "reason": "fail" if bad else "fine"}
        return ModelResponse(parts=[ToolCallPart("final_result", flags)])

    return FunctionModel(fn)


def test_model_for_scripts_every_case(pack):
    for c in load_cases(pack) + SYNTHETIC:
        assert model_for(c, pack) is not None


def test_deterministic_tier_end_to_end(tmp_path):
    res = run(tier="deterministic", out_dir=tmp_path, readme=tmp_path / "README.md")
    sc = res.scorecard
    assert res.ok and res.reasons == [] and res.problems == []
    assert sc.tier == "deterministic" and sc.families["redteam"].rate == 1.0 and sc.families["scope"].rate == 1.0
    assert sc.families["factual"].rate == 1.0 and sc.families["multiturn"].rate == 1.0 and sc.families["paraphrase"].rate == 1.0
    assert sc.cases["synthetic_repair"] is True and sc.cases["synthetic_fallback_429"] is True
    assert sc.safe_sql_rate == 100.0 and sc.invariants.passed == sc.invariants.total > 0
    assert sc.dimensions["quality"] is None and sc.judge_reliable is False  # no judge in this tier
    assert (tmp_path / "latest.md").exists() and (tmp_path / "latest.json").exists()
    assert not (tmp_path / "README.md").exists()  # badge only touched for the default model-tier report dir


def test_debug_flag_logs_each_case_with_progress_and_timing(caplog, tmp_path):
    with caplog.at_level("DEBUG", logger="evals"):
        run(tier="deterministic", out_dir=tmp_path, readme=tmp_path / "R.md", families={"scope"}, limit=1, debug=True)
    msgs = [r.message for r in caplog.records]
    assert any("[1/1] sc_weather (scope):" in m for m in msgs)
    assert any(m.startswith("[1/1] sc_weather done in") and "refused=" in m for m in msgs)


def test_check_only(tmp_path):
    res = run(check_only=True, out_dir=tmp_path)
    assert res.problems == [] and res.ok and res.scorecard is None


def test_family_filter_and_limit(tmp_path):
    res = run(tier="deterministic", families={"redteam"}, limit=3, out_dir=tmp_path, readme=tmp_path / "R.md")
    assert set(res.scorecard.cases) == {"rt_ignore_instructions", "rt_ignore_instructions_reworded", "rt_ignore_instructions_fullwidth"}


def test_baseline_update_and_gate(tmp_path):
    run(tier="deterministic", out_dir=tmp_path, baseline_update=True, readme=tmp_path / "R.md")
    assert (tmp_path / "baseline.json").exists()
    res = run(tier="deterministic", out_dir=tmp_path, readme=tmp_path / "R.md")
    assert res.ok and res.scorecard.flips == {"regressed": [], "fixed": []}


def test_model_tier_without_key_is_harness_error(monkeypatch, tmp_path):
    for k in ("AGENT_LLM_BEARER_TOKEN", "JUDGE_LLM_BEARER_TOKEN", "OPENROUTER_API_KEY", "LLM_BEARER_TOKEN", "LLM_JUDGE_BEARER_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    res = run(tier="model", out_dir=tmp_path, readme=tmp_path / "R.md")
    assert not res.ok and res.problems == ["no model API key configured (AGENT_LLM_BEARER_TOKEN)"]


def test_run_result_carries_the_baseline_it_gated_against(tmp_path):
    run(tier="deterministic", out_dir=tmp_path, baseline_update=True, readme=tmp_path / "R.md")
    res = run(tier="deterministic", out_dir=tmp_path, readme=tmp_path / "R.md")
    assert res.baseline is not None and res.baseline.overall == res.scorecard.overall


def test_model_tier_end_to_end_narrative(monkeypatch, tmp_path):
    """Regression test for the loop-safe judge fix: pydantic-evals calls CalibratedJudge from the
    running event loop's thread, so a judge that only implements sync `evaluate()` via `run_sync`
    used to blow up with 'this event loop is already running' on every real dataset run, turning
    every narrative case into a judge_error verdict (quality 0). Before evals/judge.py grew
    `evaluate_async` (anyio.to_thread.run_sync), this test fails; after, it passes."""
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "test-key")
    monkeypatch.setattr(run_module, "build_model", lambda: _scripted_agent_model())
    monkeypatch.setattr(run_module, "build_judge_model", lambda cfg: _scripted_judge_model())
    from adpilot.core.audit import MemorySink

    tmp_readme = tmp_path / "README.md"
    res = run(tier="model", families={"narrative"}, repeat=1, out_dir=tmp_path, readme=tmp_readme, audit=MemorySink())
    sc = res.scorecard
    assert sc is not None
    assert sc.families["narrative"].rate == 1.0
    assert sc.dimensions["quality"] == 100.0
    assert sc.judge_reliable is True
    assert "JudgeError" not in sc.failures_by_kind
    assert sc.calls_used > 0


def test_model_tier_badge_updates_readme_with_relative_out_dir(monkeypatch, tmp_path):
    """Regression test for the badge-path fix: `out_dir == MODEL_REPORTS` compared a relative Path
    (what the nightly workflow and README pass via --out) to an absolute path, so the badge never
    updated. Patch MODEL_REPORTS to a temp dir and pass a *relative* out_dir that resolves to it."""
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "test-key")
    monkeypatch.setattr(run_module, "build_model", lambda: _scripted_agent_model())
    monkeypatch.setattr(run_module, "build_judge_model", lambda cfg: _scripted_judge_model())
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(run_module, "MODEL_REPORTS", reports_dir)
    tmp_readme = tmp_path / "README.md"
    tmp_readme.write_text("before\n![evals](https://img.shields.io/badge/evals-pending-lightgrey)\nafter\n")
    from adpilot.core.audit import MemorySink

    rel_out_dir = os.path.relpath(reports_dir)
    res = run(tier="model", families={"narrative"}, repeat=1, out_dir=rel_out_dir, readme=tmp_readme, audit=MemorySink())
    assert res.scorecard is not None
    text = tmp_readme.read_text()
    assert "evals-pending-lightgrey" not in text
    assert Path(rel_out_dir).resolve() == reports_dir.resolve()


def test_model_tier_writes_calls_scores_and_run_to_the_sink(monkeypatch, tmp_path, pack):
    from adpilot.core.audit import MemorySink

    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "test-key")
    monkeypatch.setattr(run_module, "build_model", lambda: _scripted_agent_model())
    monkeypatch.setattr(run_module, "build_judge_model", lambda cfg: _scripted_judge_model())
    sink = MemorySink()
    res = run(tier="model", families={"narrative"}, repeat=1, out_dir=tmp_path, readme=tmp_path / "R.md", audit=sink)
    sc = res.scorecard
    assert res.run_id and res.run_id.startswith("run_") and sc.run_id == res.run_id and res.audit is not None and res.audit.ok
    agent_rows = [r for r in sink.calls if r.source == "eval"]
    judge_rows = [r for r in sink.calls if r.source == "judge"]
    n_narr = len(load_cases(pack, {"narrative"}))
    assert len(agent_rows) == n_narr and all(r.run_id == res.run_id and r.family == "narrative" and r.case_name for r in agent_rows)
    assert len(judge_rows) == n_narr + 52  # one per narrative case + the 52 calibration entries
    assert all(r.run_id == res.run_id and r.case_name and r.family in ("narrative", "calibration") for r in judge_rows)
    assert {s.name for s in sink.scores} >= {"judge", "judge_pass", "safe_sql", "calls_ok"}
    judge_scores = [s for s in sink.scores if s.name == "judge"]
    assert judge_scores and all(s.source == "judge" and s.grader and s.passed is True for s in judge_scores)
    assert {s.trace_id for s in sink.scores} == {r.trace_id for r in agent_rows}
    assert len(sink.runs) == 1 and sink.runs[0].run_id == res.run_id and sink.runs[0].overall == sc.overall
    assert sc.calls_used == sum(r.requests for r in sink.calls) and sc.calls_used > 0
    assert sink.flushed == {"agent_calls": n_narr + n_narr + 52, "scores": len(sink.scores), "eval_runs": 1}  # agent + judge rows


def test_deterministic_tier_uses_memory_sink_and_stamps_run_id(tmp_path):
    res = run(tier="deterministic", families={"scope"}, limit=2, out_dir=tmp_path, readme=tmp_path / "R.md")
    assert res.run_id and res.scorecard.run_id == res.run_id and res.audit is not None and res.audit.written["agent_calls"] == 2


def test_model_tier_audit_unavailable_is_a_harness_error(monkeypatch, tmp_path):
    from adpilot.core.audit import AuditUnavailable

    class Broken:
        def preflight(self):
            raise AuditUnavailable("credentials", "no creds")

    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "test-key")
    monkeypatch.setattr(run_module, "build_model", lambda: _scripted_agent_model())
    res = run(tier="model", families={"scope"}, limit=1, out_dir=tmp_path, readme=tmp_path / "R.md", audit=Broken())
    assert not res.ok and res.scorecard is None and res.problems == ["audit unavailable (credentials): no creds"]


def test_unflushed_rows_fail_the_run_but_reports_are_written(monkeypatch, tmp_path):
    from adpilot.core.audit import FlushReport, MemorySink

    class Flaky(MemorySink):
        reports_existed_at_flush: bool | None = None

        def flush(self):
            self.reports_existed_at_flush = (tmp_path / "latest.json").exists()
            super().flush()
            return FlushReport(written={}, failed={"agent_calls": 2}, errors=["agent_calls: 503"])

    sink = Flaky()
    res = run(tier="deterministic", families={"scope"}, limit=2, out_dir=tmp_path, readme=tmp_path / "R.md", audit=sink)
    assert not res.ok and res.audit.pending == 2 and (tmp_path / "latest.json").exists() and res.scorecard is not None
    assert sink.reports_existed_at_flush is True  # flush must run after write_reports, not before


def test_scores_from_report_warns_when_a_raised_case_has_no_trace_id(caplog):
    """A case whose task raised has output is None, so it has no agent_calls row to key scores on —
    dropping it is correct, but it must not vanish with zero trace. Fake report/case: scores_from_report
    only touches .cases[].{output,name,assertions,scores,evaluator_failures} via getattr/duck typing."""
    from types import SimpleNamespace

    from evals.run import scores_from_report

    raised_case = SimpleNamespace(name="rt_drop_table", output=None, assertions={}, scores={}, evaluator_failures=[])
    ok_case = SimpleNamespace(
        name="sc_weather",
        output=SimpleNamespace(trace_id="t1"),
        assertions={"factual": SimpleNamespace(value=True, reason=None)},
        scores={},
        evaluator_failures=[],
    )
    report = SimpleNamespace(cases=[raised_case, ok_case])

    with caplog.at_level("WARNING", logger="evals.run"):
        rows = scores_from_report(report, "run_1", None)

    assert [r.trace_id for r in rows] == ["t1"]  # the raised case contributed nothing
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("rt_drop_table" in m for m in warnings)


def test_guarded_redteam_case_gets_a_compliant_model(pack):
    from pydantic_ai.models.function import FunctionModel

    from evals.cases import load_cases

    by_name = {c.name: c for c in load_cases(pack, {"redteam"})}
    guarded, unguarded = by_name["rt_information_schema"], by_name["rt_pii_fishing"]
    assert guarded.expected.guard and not unguarded.expected.guard
    # A guarded case must be refused by layer 0; the scripted model would comply, so only the gate can make it pass.
    m = model_for(guarded, pack)
    assert isinstance(m, FunctionModel)
    parts = m.function([_user_msg(guarded.question)], None).parts
    assert parts[0].tool_name == "run_sql"
    parts = model_for(unguarded, pack).function([_user_msg(unguarded.question)], None).parts
    assert parts[0].tool_name == "final_result_Refusal"


def _user_msg(text):
    from pydantic_ai import ModelRequest, UserPromptPart

    return ModelRequest(parts=[UserPromptPart(content=text)])


def test_classifier_cases_are_skipped_unless_the_model_tier_has_the_flag(eval_duck, pack, monkeypatch, caplog):
    monkeypatch.delenv("ADPILOT_INPUT_CLASSIFIER", raising=False)
    res = run(tier="deterministic", families={"redteam"})
    assert res.scorecard is not None
    assert not any("_l1" in name for name in res.scorecard.cases), "classifier cases must not run (or count) without layer 1"
    assert "skipping" in caplog.text and "classifier" in caplog.text


def test_grader_records_the_judge_that_actually_ran(monkeypatch, tmp_path):
    """The A/B on 2026-09-21 compared two runs whose scorecards named different judges, but the
    'current' run's judge served 0 of 30 calls and silently fell back to the agent's own model:
    `scores.grader` and `models["judge"]` both recorded the *requested* judge, so the self-grading
    was invisible. Both must name the model that answered."""
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "test-key")
    monkeypatch.setenv("JUDGE_LLM_TARGET_MODEL", "vendor/requested-judge")
    monkeypatch.setattr(run_module, "build_model", lambda: _scripted_agent_model())
    monkeypatch.setattr(run_module, "build_judge_model", lambda cfg: _scripted_judge_model())
    from adpilot.core.audit import MemorySink

    sink = MemorySink()
    res = run(tier="model", families={"narrative"}, repeat=1, out_dir=tmp_path, readme=tmp_path / "README.md", audit=sink)

    used = {r.model_used for r in sink.calls if r.source == "judge" and r.model_used}
    assert used and "vendor/requested-judge" not in used
    graders = {s.grader for s in sink.scores if s.source == "judge"}
    assert graders == used, f"grader should name the model that answered, got {graders}"
    assert res.scorecard.models["judge_used"] == ", ".join(sorted(used))
    assert res.scorecard.models["judge"] == "vendor/requested-judge"
