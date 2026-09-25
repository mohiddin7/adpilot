from pathlib import Path

import yaml

BRIEF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "brief-daily.yml"


def _steps():
    wf = yaml.safe_load(BRIEF.read_text())
    return [s for job in wf["jobs"].values() for s in job["steps"]]


def test_brief_reads_bigquery_with_real_credentials():
    steps = _steps()
    auth = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("google-github-actions/auth@")]
    brief = [i for i, s in enumerate(steps) if "adpilot brief" in str(s.get("run", ""))]
    assert auth and brief and auth[0] < brief[0]
    env = steps[brief[0]].get("env", {})
    assert env.get("ADPILOT_CONNECTOR") == "bigquery"
    assert {"BQ_PROJECT_ID", "BQ_STAGING_DATASET", "BQ_PRODUCTION_DATASET"} <= set(env)


def test_freshness_gate_runs_even_after_a_failed_brief():
    gate = [s for s in _steps() if s.get("name") == "Data freshness gate"]
    assert gate and gate[0].get("if") == "always()" and "fct_unified_marketing_performance" in gate[0]["run"]


def test_freshness_gate_also_requires_todays_pipeline_run_to_have_passed():
    # 02 commits gold before 03/04/05/07, so MAX(date) alone passes a run that failed later.
    gate = [s for s in _steps() if s.get("name") == "Data freshness gate"][0]
    assert "tbl_pipeline_runs" in gate["run"] and "BQ_STAGING_DATASET" in gate.get("env", {})
