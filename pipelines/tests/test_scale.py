"""Scale, cost and reliability contracts from spec §5.1 that hold across the pipeline scripts."""
from pathlib import Path

import pytest

PIPELINES = Path(__file__).resolve().parent.parent
SCRIPTS = ["01_validate_and_ingest", "02_run_transformations", "03_anomaly_detection",
           "04_budget_optimizer", "05_forecast", "07_qa_validation"]


def _src(stem: str) -> str:
    return (PIPELINES / f"{stem}.py").read_text()


@pytest.mark.parametrize("stem", SCRIPTS)
def test_clients_come_only_from_common(stem):
    assert "bigquery.Client(" not in _src(stem)          # common.bq_client() labels every job


@pytest.mark.parametrize("stem", ["01_validate_and_ingest", "03_anomaly_detection", "04_budget_optimizer", "05_forecast"])
def test_dataframe_loads_are_explicit_parquet(stem):
    src = _src(stem)
    assert "bigquery.LoadJobConfig(" not in src and "common.parquet_load_config(" in src


@pytest.mark.parametrize("stem", SCRIPTS)
def test_no_script_writes_log_files_itself(stem):
    # Cloud Run functions: only /tmp is writable. Logging goes through common.log_handlers, which skips the file.
    src = _src(stem)
    assert "FileHandler(" not in src and "makedirs(" not in src and ".mkdir(parents=True" not in src


def test_function_source_is_self_contained():
    # The functions deploy from --source pipelines: everything they import or read must live under pipelines/.
    reqs = (PIPELINES / "requirements.txt").read_text()
    for pkg in ("functions-framework", "google-cloud-bigquery", "google-cloud-storage", "db-dtypes", "pandas",
                "numpy", "pyarrow", "scipy", "statsmodels", "python-dotenv"):
        assert pkg in reqs, pkg
    ignore = (PIPELINES / ".gcloudignore").read_text().split()
    assert "tests/" in ignore and "logs/" in ignore
    assert (PIPELINES / "calibration" / "levels.json").exists() and (PIPELINES / "calibration" / "seasonality.json").exists()
