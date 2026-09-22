"""Daily data run on Cloud Run functions (Phase 3D). Deploy and operate: docs/deploy.md.

generate_daily  HTTP, called by Cloud Scheduler at 02:00 UTC: writes landing/<as_of>/ (three CSVs, then the manifest).
run_pipeline    GCS object.finalized, on a batch manifest only: verifies the batch and runs 01 → 07.

  python pipelines/main.py backfill              # one batch, 2024-01-31 … yesterday, through the function path
  python pipelines/main.py run gs://B/landing/<as_of>/_manifest.json   # run a batch locally with the same code
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import common  # noqa: E402

try:
    import functions_framework

    http, cloud_event = functions_framework.http, functions_framework.cloud_event
except ImportError:  # tests and local runs
    def http(f):
        return f

    cloud_event = http

gen = common.load_script("00_generate_synthetic")

LANDING = "landing/"
MANIFEST = "_manifest.json"
STATUS = "_status.json"
MAX_CATCHUP_DAYS = 31


def utc_today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def batch_window(gold_max: date | None, today: date) -> tuple[date, date]:
    """Dates a batch built on `today` carries: whatever gold lacks, plus the still-restating dates and the one turning
    final (today − RESTATING_DAYS − 1 … today − 1). Refuses gaps the daily run must not paper over."""
    if gold_max is None:
        raise RuntimeError("gold is empty: run `python pipelines/main.py backfill` first")
    end = today - timedelta(days=1)
    start = min(gold_max + timedelta(days=1), end - timedelta(days=common.RESTATING_DAYS))
    days = (end - start).days + 1
    if days > MAX_CATCHUP_DAYS:
        raise RuntimeError(f"gold ends {gold_max}: a {days}-day gap needs the backfill, not the daily run")
    return start, end


def _rows(body: bytes) -> int:
    return max(len(body.splitlines()) - 1, 0)


def build_batch(start: date, end: date, as_of: date, *, today: date | None = None) -> dict[str, bytes]:
    """The batch's files, manifest included. Same arguments → same bytes."""
    files = {gen.PLATFORMS[p]["out"]: df.to_csv(index=False).encode()
             for p, df in gen.generate(start, end, as_of, today=today).items()}
    manifest = {"batch": as_of.isoformat(), "start": start.isoformat(), "end": end.isoformat(), "as_of": as_of.isoformat(),
                "generator_version": gen.GENERATOR_VERSION,
                "files": [{"name": n, "rows": _rows(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in sorted(files.items())]}
    return files | {MANIFEST: json.dumps(manifest, indent=1).encode()}


def verify_batch(manifest: dict, files: dict[str, bytes]) -> None:
    """Fail closed before any BigQuery work: a partial or corrupt upload never reaches bronze."""
    listed = {f["name"] for f in manifest["files"]}
    if listed != set(files):
        raise RuntimeError(f"batch files {sorted(files)} != manifest {sorted(listed)}")
    for f in manifest["files"]:
        body = files[f["name"]]
        if hashlib.sha256(body).hexdigest() != f["sha256"]:
            raise RuntimeError(f"{f['name']}: sha256 does not match the manifest")
        if _rows(body) != f["rows"]:
            raise RuntimeError(f"{f['name']}: {_rows(body)} rows, manifest says {f['rows']}")


def _bucket(name: str | None = None):
    from google.cloud import storage

    return storage.Client(project=common.PROJECT).bucket(name or os.environ["RAW_BUCKET"])


def upload_batch(bucket, files: dict[str, bytes]) -> str:
    """Manifest last: its arrival is the signal that the batch is complete."""
    prefix = f"{LANDING}{json.loads(files[MANIFEST])['batch']}/"
    for name in sorted(n for n in files if n != MANIFEST) + [MANIFEST]:
        kind = "application/json" if name.endswith(".json") else "text/csv"
        bucket.blob(prefix + name).upload_from_string(files[name], content_type=kind)
    return prefix + MANIFEST


def gold_max_date() -> date | None:
    from google.api_core.exceptions import NotFound

    gold = f"{common.PROJECT}.{common.PRODUCTION_DS}.fct_unified_marketing_performance"
    try:
        rows = list(common.bq_client().query(f"SELECT MAX(date) AS d FROM `{gold}`").result())
        return rows[0].d if rows else None
    except NotFound:
        return None


@http
def generate_daily(request=None) -> dict:
    today = utc_today()
    os.environ["PIPELINE_RUN_ID"], os.environ["PIPELINE_STEP"] = f"generate-{today}", "generate"
    start, end = batch_window(gold_max_date(), today)
    path = upload_batch(_bucket(), build_batch(start, end, as_of=today))
    common.get_logger("generate_daily").info("wrote %s for %s … %s", path, start, end)
    return {"manifest": path, "start": start.isoformat(), "end": end.isoformat()}


def backfill(bucket_name: str | None = None) -> str:
    """One batch from HISTORY_START to yesterday, as_of today like any daily batch. The first scheduled run then
    finalises its two restating days."""
    today = utc_today()
    return upload_batch(_bucket(bucket_name), build_batch(gen.HISTORY_START, today - timedelta(days=1), as_of=today))
