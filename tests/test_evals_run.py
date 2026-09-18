import os
from pathlib import Path

from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import evals.run as run_module
from evals.cases import load_cases
from evals.deterministic import SYNTHETIC, model_for
from evals.run import run

# Answers that the judge-calibration set (packs/ads/judge_calibration.yaml) labels "fail" — a substring
# unique to each bad answer. A judge that flags these and passes everything else agrees 100% with the
# calibration labels, so the judge is reported reliable and its quality score can be exercised end to end.
_CALIBRATION_FAIL_MARKERS = (
    "$98.4K",
    "TikTok is the most efficient",
    "roughly balanced",
    "4,218 conversions",
    "will spend exactly $4,975",
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
    assert set(res.scorecard.cases) == {"rt_ignore_instructions", "rt_drop_table", "rt_update_rows"}


def test_baseline_update_and_gate(tmp_path):
    run(tier="deterministic", out_dir=tmp_path, baseline_update=True, readme=tmp_path / "R.md")
    assert (tmp_path / "baseline.json").exists()
    res = run(tier="deterministic", out_dir=tmp_path, readme=tmp_path / "R.md")
    assert res.ok and res.scorecard.flips == {"regressed": [], "fixed": []}


def test_model_tier_without_key_is_harness_error(monkeypatch, tmp_path):
    for k in ("OPENROUTER_API_KEY", "LLM_BEARER_TOKEN", "LLM_JUDGE_BEARER_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    res = run(tier="model", out_dir=tmp_path, readme=tmp_path / "R.md")
    assert not res.ok and res.problems == ["no model API key configured (OPENROUTER_API_KEY or LLM_BEARER_TOKEN)"]


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
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(run_module, "build_model", lambda: _scripted_agent_model())
    monkeypatch.setattr(run_module, "build_judge_model", lambda cfg: _scripted_judge_model())
    tmp_readme = tmp_path / "README.md"
    res = run(tier="model", families={"narrative"}, repeat=1, out_dir=tmp_path, readme=tmp_readme)
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
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(run_module, "build_model", lambda: _scripted_agent_model())
    monkeypatch.setattr(run_module, "build_judge_model", lambda cfg: _scripted_judge_model())
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(run_module, "MODEL_REPORTS", reports_dir)
    tmp_readme = tmp_path / "README.md"
    tmp_readme.write_text("before\n![evals](https://img.shields.io/badge/evals-pending-lightgrey)\nafter\n")
    rel_out_dir = os.path.relpath(reports_dir)
    res = run(tier="model", families={"narrative"}, repeat=1, out_dir=rel_out_dir, readme=tmp_readme)
    assert res.scorecard is not None
    text = tmp_readme.read_text()
    assert "evals-pending-lightgrey" not in text
    assert Path(rel_out_dir).resolve() == reports_dir.resolve()
