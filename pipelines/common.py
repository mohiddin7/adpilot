"""Shared config and plumbing for pipeline scripts. Real names come from .env; defaults are neutral."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
from pathlib import Path
from types import ModuleType

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")

PROJECT       = os.getenv("BQ_PROJECT_ID", "adpilot-lakehouse")
BRONZE_DS     = os.getenv("BQ_BRONZE_DATASET", "adpilot_bronze")
STAGING_DS    = os.getenv("BQ_STAGING_DATASET", "adpilot_staging")
PRODUCTION_DS = os.getenv("BQ_PRODUCTION_DATASET", "adpilot_production")

# The newest RESTATING_DAYS dates are still gaining late conversions (85 % at age 1, 95 % at age 2, final at
# age 3). Every modelling step (03, 04, 05) ends its window before them.
RESTATING_DAYS = 2
LOG_DIR = HERE / "logs"


def mature_through_sql(gold_ref: str) -> str:
    """SQL expression for the newest date whose conversions are final."""
    return f"DATE_SUB((SELECT MAX(date) FROM `{gold_ref}`), INTERVAL {RESTATING_DAYS} DAY)"


class _JsonFormatter(logging.Formatter):
    """One JSON object per line: Cloud Logging indexes severity and the run/step labels."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "logger": record.name, "message": record.getMessage(),
                 "pipeline_run": os.getenv("PIPELINE_RUN_ID", ""), "step": os.getenv("PIPELINE_STEP", "")}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def log_handlers(file_stem: str, fmt: str | None = None) -> list[logging.Handler]:
    """stdout always (JSON under Cloud Run, where K_SERVICE is set); a file in LOG_DIR only when writable.
    `fmt` overrides the plain-text format string (ignored under Cloud Run — the JSON formatter already
    carries `logger`)."""
    formatter = _JsonFormatter() if os.getenv("K_SERVICE") else logging.Formatter(
        fmt or "%(asctime)s %(levelname)-8s %(message)s", "%Y-%m-%dT%H:%M:%S")
    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.INFO)
    handlers: list[logging.Handler] = [out]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_DIR / f"{file_stem}.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        handlers.append(file_handler)
    except OSError:
        pass  # read-only filesystem (Cloud Run functions): stdout only
    for h in handlers:
        h.setFormatter(formatter)
    return handlers


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        log.setLevel(logging.DEBUG)
        log.propagate = False  # otherwise 01's root basicConfig duplicates every 02-05 line on stdout and into its own log
        for h in log_handlers(name):
            log.addHandler(h)
    return log


def _label(value: str) -> str:
    """BigQuery label values: lowercase letters, digits, _ and -; at most 63 characters."""
    return re.sub(r"[^a-z0-9_-]", "-", value.lower())[:63] or "none"


def job_labels() -> dict[str, str]:
    return {"pipeline_run": _label(os.getenv("PIPELINE_RUN_ID", "manual")),
            "step": _label(os.getenv("PIPELINE_STEP", "manual"))}


def bq_client():
    """The only way pipeline code builds a BigQuery client. Every job it runs carries pipeline_run/step labels, so
    INFORMATION_SCHEMA.JOBS attributes bytes, slot time and latency to one run and one step."""
    from google.cloud import bigquery

    labels = job_labels()
    return bigquery.Client(project=PROJECT,
                           default_query_job_config=bigquery.QueryJobConfig(labels=labels),
                           default_load_job_config=bigquery.LoadJobConfig(labels=labels))


def parquet_load_config(**kwargs):
    """DataFrame → BigQuery loads. Explicit Parquet keeps types exact, and a missing pyarrow fails loudly instead of
    degrading to CSV."""
    from google.cloud import bigquery

    return bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.PARQUET, **kwargs)


def load_script(stem: str) -> ModuleType:
    """Import a pipeline script by file stem (digit-prefixed names included), once per process."""
    if stem in sys.modules:
        return sys.modules[stem]
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location(stem, HERE / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[stem] = mod
    spec.loader.exec_module(mod)
    return mod
