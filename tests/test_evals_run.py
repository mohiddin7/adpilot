from evals.cases import load_cases
from evals.deterministic import SYNTHETIC, model_for
from evals.run import run


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
