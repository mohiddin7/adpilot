import importlib
import json
import logging
import sys
from pathlib import Path

from google.auth.credentials import AnonymousCredentials

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_common_reads_env(monkeypatch):
    monkeypatch.setenv("BQ_PROJECT_ID", "p1")
    monkeypatch.setenv("BQ_BRONZE_DATASET", "b1")
    monkeypatch.setenv("BQ_STAGING_DATASET", "s1")
    monkeypatch.setenv("BQ_PRODUCTION_DATASET", "g1")
    import common
    importlib.reload(common)
    assert (common.PROJECT, common.BRONZE_DS, common.STAGING_DS, common.PRODUCTION_DS) == ("p1", "b1", "s1", "g1")


def _fresh_common():
    import common
    return importlib.reload(common)


def _unwritable(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    return blocker / "logs"          # mkdir under a regular file fails for every user, root included


def test_log_handlers_fall_back_to_stdout_when_logs_unwritable(tmp_path, monkeypatch):
    common = _fresh_common()
    monkeypatch.setattr(common, "LOG_DIR", _unwritable(tmp_path))
    handlers = common.log_handlers("x")
    assert len(handlers) == 1 and not isinstance(handlers[0], logging.FileHandler)


def test_log_handlers_add_a_file_when_writable(tmp_path, monkeypatch):
    common = _fresh_common()
    monkeypatch.setattr(common, "LOG_DIR", tmp_path / "logs")
    handlers = common.log_handlers("x")
    assert isinstance(handlers[1], logging.FileHandler) and (tmp_path / "logs" / "x.log").exists()
    handlers[1].close()


def test_log_handlers_fmt_overrides_plain_format(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("K_SERVICE", raising=False)
    common = _fresh_common()
    monkeypatch.setattr(common, "LOG_DIR", _unwritable(tmp_path))
    log = logging.getLogger("fmt-test")
    log.handlers.clear()
    log.propagate = False
    log.setLevel(logging.INFO)
    for h in common.log_handlers("fmt-test", fmt="%(name)s|%(message)s"):
        log.addHandler(h)
    log.info("hello")
    assert capsys.readouterr().out.strip().splitlines()[-1] == "fmt-test|hello"


def test_json_logs_under_cloud_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("K_SERVICE", "run-pipeline")
    monkeypatch.setenv("PIPELINE_RUN_ID", "r1")
    monkeypatch.setenv("PIPELINE_STEP", "02_transform")
    common = _fresh_common()
    monkeypatch.setattr(common, "LOG_DIR", _unwritable(tmp_path))
    log = logging.getLogger("json-test")
    log.handlers.clear()
    log.propagate = False
    log.setLevel(logging.INFO)
    for h in common.log_handlers("json-test"):
        log.addHandler(h)
    log.warning("hello")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line == {"severity": "WARNING", "logger": "json-test", "message": "hello", "pipeline_run": "r1", "step": "02_transform"}


def test_bq_client_labels_every_job(monkeypatch):
    monkeypatch.setattr("google.auth.default", lambda *a, **k: (AnonymousCredentials(), "p"))
    monkeypatch.setenv("PIPELINE_RUN_ID", "R20260924T020001-2026-09-24")
    monkeypatch.setenv("PIPELINE_STEP", "02_transform")
    client = _fresh_common().bq_client()
    want = {"pipeline_run": "r20260924t020001-2026-09-24", "step": "02_transform"}
    assert client.default_query_job_config.labels == want
    assert client.default_load_job_config.labels == want


def test_label_sanitises_and_truncates():
    common = _fresh_common()
    assert common._label("A B/C") == "a-b-c"
    assert len(common._label("x" * 100)) == 63
    assert common._label("") == "none"


def test_parquet_load_config():
    from google.cloud import bigquery
    cfg = _fresh_common().parquet_load_config(write_disposition="WRITE_TRUNCATE")
    assert cfg.source_format == bigquery.SourceFormat.PARQUET and cfg.write_disposition == "WRITE_TRUNCATE"


def test_mature_through_sql():
    assert _fresh_common().mature_through_sql("p.d.t") == "DATE_SUB((SELECT MAX(date) FROM `p.d.t`), INTERVAL 2 DAY)"


def test_load_script_imports_digit_prefixed_scripts_once():
    common = _fresh_common()
    mod = common.load_script("03_anomaly_detection")
    assert hasattr(mod, "AnomalyDetectionEngine") and common.load_script("03_anomaly_detection") is mod


def test_get_logger_does_not_propagate_to_the_root_logger():
    assert _fresh_common().get_logger("x-prop").propagate is False
