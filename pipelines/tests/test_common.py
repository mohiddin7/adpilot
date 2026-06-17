import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_common_reads_env(monkeypatch):
    monkeypatch.setenv("BQ_PROJECT_ID", "p1")
    monkeypatch.setenv("BQ_BRONZE_DATASET", "b1")
    monkeypatch.setenv("BQ_STAGING_DATASET", "s1")
    monkeypatch.setenv("BQ_PRODUCTION_DATASET", "g1")
    import common
    importlib.reload(common)
    assert (common.PROJECT, common.BRONZE_DS, common.STAGING_DS, common.PRODUCTION_DS) == ("p1", "b1", "s1", "g1")
