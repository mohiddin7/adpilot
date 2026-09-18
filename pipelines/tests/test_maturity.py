import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402


class _Capture:
    def __init__(self):
        self.sql = []

    def get_table(self, ref):
        return object()

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        return SimpleNamespace(to_dataframe=lambda: pd.DataFrame(), result=lambda *a, **k: [])


def _fetch_sql(monkeypatch, stem, cls):
    fake = _Capture()
    monkeypatch.setattr(common, "bq_client", lambda: fake)
    mod = common.load_script(stem)
    getattr(mod, cls)()._fetch_gold_data()
    return fake.sql[-1], mod.Config.gold_ref()


def test_anomaly_window_ends_before_restating_days(monkeypatch):
    sql, _ = _fetch_sql(monkeypatch, "03_anomaly_detection", "AnomalyDetectionPipeline")
    assert f"DATE_SUB(MAX(date), INTERVAL {common.RESTATING_DAYS} DAY) AS max_d" in sql
    assert "AND bounds.max_d" in sql


@pytest.mark.parametrize("stem, cls", [("04_budget_optimizer", "BudgetOptimizerPipeline"), ("05_forecast", "ForecastPipeline")])
def test_budget_and_forecast_train_on_mature_days_only(monkeypatch, stem, cls):
    sql, gold = _fetch_sql(monkeypatch, stem, cls)
    assert f"AND {common.mature_through_sql(gold)}" in sql
