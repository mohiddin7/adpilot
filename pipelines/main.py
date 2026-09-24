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
import re
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import common  # noqa: E402

try:
    import functions_framework

    http, cloud_event = functions_framework.http, functions_framework.cloud_event
except ModuleNotFoundError:  # tests and local runs
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
    for f in manifest["files"]:
        name = f["name"]
        if Path(name).name != name or name.startswith("."):
            raise RuntimeError(f"{name}: not a plain file name")
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


STEP_NAMES = ("01_ingest", "02_transform", "03_anomalies", "04_budget", "05_forecast", "07_qa")
_MANIFEST_NAME = re.compile(rf"{LANDING}[^/]+/{re.escape(MANIFEST)}")


@dataclass
class StepResult:
    step: str
    started_at: str
    seconds: float
    ok: bool
    rows: int = 0
    error: str = ""


def is_batch_manifest(name: str) -> bool:
    return bool(_MANIFEST_NAME.fullmatch(name))


# Heavy modules (scipy, statsmodels) load inside the step that needs them, which keeps cold starts short.
def _ingest(batch_dir: Path, manifest: dict) -> int:
    pipe = common.load_script("01_validate_and_ingest").IngestionPipeline()
    rows = 0
    for f in manifest["files"]:
        result = pipe.run(str(batch_dir / f["name"]))
        if not result.success:
            raise RuntimeError(f"{f['name']}: {result.error_stage}: {result.error_message}")
        rows += result.rows_accepted
    return rows


def _transform(batch_dir: Path, manifest: dict) -> int:
    t = common.load_script("02_run_transformations")
    t.GoldTransformer(common.bq_client(), t.setup_logging(Path("02_run_transformations.log")),
                      (manifest["start"], manifest["end"])).run()
    return 0


def _anomalies(batch_dir: Path, manifest: dict) -> int:
    common.load_script("03_anomaly_detection").AnomalyDetectionPipeline().run()
    return 0


def _budget(batch_dir: Path, manifest: dict) -> int:
    common.load_script("04_budget_optimizer").BudgetOptimizerPipeline().run()
    return 0


def _forecast(batch_dir: Path, manifest: dict) -> int:
    common.load_script("05_forecast").ForecastPipeline().run()
    return 0


def _qa(batch_dir: Path, manifest: dict) -> int:
    if common.load_script("07_qa_validation").QAValidationRunner().run() != 0:
        raise RuntimeError("QA found critical failures (see the 07_qa step log)")
    return 0


STEPS: tuple[tuple[str, Callable[[Path, dict], int]], ...] = tuple(
    zip(STEP_NAMES, (_ingest, _transform, _anomalies, _budget, _forecast, _qa)))


def run_batch(read: Callable[[str], bytes], write: Callable[[str, bytes], None], manifest_name: str, *,
              steps=STEPS, record=None, gold_max: Callable[[], date | None] | None = None,
              now: datetime | None = None) -> list[StepResult]:
    """Verify the batch, then run each step in order and stop at the first failure. Always writes _status.json (and
    the metrics, when `record` is given). Re-raises the failure so the event shows as failed.

    With `gold_max`, a batch ending before gold's newest date is refused: no later window carries its restating
    dates again, so replaying it would leave them at 85–95 % maturity for good."""
    t_start = time.perf_counter()
    prefix = manifest_name.rsplit("/", 1)[0] + "/"
    run_id = f"r{(now or datetime.now(timezone.utc)):%Y%m%dt%H%M%S}-{prefix.split('/')[-2]}"
    prev_run_id, prev_step = os.environ.get("PIPELINE_RUN_ID"), os.environ.get("PIPELINE_STEP")
    os.environ["PIPELINE_RUN_ID"] = run_id  # set before verification too, so a failed run's own metrics query is still labelled
    results: list[StepResult] = []
    manifest, error = None, ""
    try:
        manifest = json.loads(read(manifest_name))
        files = {f["name"]: read(prefix + f["name"]) for f in manifest["files"]}
        verify_batch(manifest, files)
        if gold_max and os.environ.get("PIPELINE_ALLOW_OLD_BATCH") != "1":
            os.environ["PIPELINE_STEP"] = "verify"
            newest = gold_max()
            if newest and date.fromisoformat(manifest["end"]) < newest:
                raise RuntimeError(f"batch ends {manifest['end']} but gold already reaches {newest}: replaying it would "
                                   "regress restated conversions; set PIPELINE_ALLOW_OLD_BATCH=1 for a deliberate rebuild")
        with tempfile.TemporaryDirectory() as tmp:
            batch_dir = Path(tmp)
            for name, body in files.items():
                (batch_dir / name).write_bytes(body)
            for name, fn in steps:
                os.environ["PIPELINE_STEP"] = name
                started, t0 = datetime.now(timezone.utc).isoformat(), time.perf_counter()
                try:
                    rows = fn(batch_dir, manifest)
                except Exception as exc:
                    results.append(StepResult(name, started, round(time.perf_counter() - t0, 3), False,
                                              error=f"{type(exc).__name__}: {exc}"[:1000]))
                    raise
                results.append(StepResult(name, started, round(time.perf_counter() - t0, 3), True, int(rows or 0)))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:1000]
        raise
    finally:
        status = {"ok": not error, "run_id": run_id, "batch": prefix, "steps": [asdict(r) for r in results], "error": error}
        if record:
            try:
                status |= record(run_id, prefix, manifest, results)  # may replace status["steps"] with the richer metrics rows
            except Exception as exc:  # metrics must never mask the pipeline's own outcome
                status |= getattr(exc, "partial_status", {})  # what record() gathered before the append failed
                status["metrics_error"] = f"{type(exc).__name__}: {exc}"[:500]
                common.get_logger("run_pipeline").error("metrics failed: %s", status["metrics_error"])
        status["seconds"] = round(time.perf_counter() - t_start, 3)
        try:
            write(prefix + STATUS, json.dumps(status, indent=1).encode())
        except Exception as exc:
            common.get_logger("run_pipeline").error("status write failed: %s: %s", type(exc).__name__, exc)
            if not error:
                raise  # a failed run's own exception is the one that propagates
        finally:
            for var, prev in (("PIPELINE_RUN_ID", prev_run_id), ("PIPELINE_STEP", prev_step)):
                if prev is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = prev
    return results


_USAGE_SQL = """
SELECT (SELECT value FROM UNNEST(labels) WHERE key = 'step') AS step, COUNT(*) AS jobs,
       SUM(IFNULL(total_bytes_billed, 0)) AS bytes_billed, SUM(IFNULL(total_slot_ms, 0)) AS slot_ms,
       COUNTIF(cache_hit) AS cache_hits
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_USER
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)
  AND parent_job_id IS NULL
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'pipeline_run' AND value = @run)
GROUP BY step"""
RUNS_SCHEMA = (("run_id", "STRING"), ("batch", "STRING"), ("step", "STRING"), ("started_at", "TIMESTAMP"),
               ("seconds", "FLOAT64"), ("ok", "BOOL"), ("rows", "INT64"), ("error", "STRING"),
               ("jobs", "INT64"), ("bytes_billed", "INT64"), ("slot_ms", "INT64"), ("cache_hits", "INT64"))


def metrics_rows(run_id: str, batch: str, results: list[StepResult], usage: dict[str, dict]) -> list[dict]:
    zero = {"jobs": 0, "bytes_billed": 0, "slot_ms": 0, "cache_hits": 0}
    return [{"run_id": run_id, "batch": batch, **asdict(r), **usage.get(common._label(r.step), zero)} for r in results]


def job_usage(client, run_id: str) -> dict[str, dict]:
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("run", "STRING", common._label(run_id))])
    return {r.step: {"jobs": r.jobs, "bytes_billed": r.bytes_billed, "slot_ms": r.slot_ms, "cache_hits": r.cache_hits}
            for r in client.query(_USAGE_SQL, job_config=cfg).result()}


def append_runs(client, rows: list[dict]) -> None:
    """One row per step into tbl_pipeline_runs (DAY-partitioned on started_at, created on first use)."""
    if not rows:
        return
    import pandas as pd
    from google.cloud import bigquery

    ref = f"{common.PROJECT}.{common.STAGING_DS}.tbl_pipeline_runs"
    table = bigquery.Table(ref, schema=[bigquery.SchemaField(n, t) for n, t in RUNS_SCHEMA])
    table.time_partitioning = bigquery.TimePartitioning(field="started_at")
    client.create_table(table, exists_ok=True)
    df = pd.DataFrame(rows, columns=[n for n, _ in RUNS_SCHEMA]).assign(started_at=lambda d: pd.to_datetime(d.started_at, utc=True))
    client.load_table_from_dataframe(df, ref, job_config=common.parquet_load_config(
        schema=table.schema, write_disposition="WRITE_APPEND")).result()


def detector_window(manifest: dict) -> tuple[date, date] | None:
    """Dates that 03 scored as mature for the first time in this run (it scores ≤ gold max − RESTATING_DAYS, 90 days back)."""
    end = date.fromisoformat(manifest["end"]) - timedelta(days=common.RESTATING_DAYS)
    start = max(date.fromisoformat(manifest["start"]), end - timedelta(days=89))
    return (start, end) if start <= end else None


def detector_check(client, manifest: dict) -> dict:
    """Live ground truth: of the anomalies planted in the newly scored dates, how many did 03 flag, same direction."""
    window = detector_window(manifest)
    if window is None:
        return {}
    truth = gen.planted(*window).assign(date=lambda t: t["date"].astype(str))
    flags = f"{common.PROJECT}.{common.STAGING_DS}.fct_anomaly_flags"
    flagged = client.query(
        f"SELECT CAST(date AS STRING) AS date, campaign_id, anomaly_direction AS direction FROM `{flags}` "
        f"WHERE is_anomaly = 1 AND date BETWEEN '{window[0]}' AND '{window[1]}'").to_dataframe()
    hits = truth.merge(flagged, on=["date", "campaign_id", "direction"])
    return {"detector_window": [w.isoformat() for w in window], "planted": len(truth),
            "planted_flagged": len(hits), "flagged": len(flagged)}


def record(run_id: str, batch: str, manifest: dict | None, results: list[StepResult]) -> dict:
    """Metrics rows are a superset of StepResult (jobs/bytes_billed/slot_ms/cache_hits added), so `run_batch`
    merges them straight into `status["steps"]` — the same numbers land in _status.json and tbl_pipeline_runs.
    Usage and the detector check fail soft (`usage_error`, `detector_error`), so the rows are always appended. An
    append failure re-raises with the gathered fields on `exc.partial_status`, which run_batch keeps."""
    os.environ["PIPELINE_STEP"] = "metrics"
    log = common.get_logger("run_pipeline")
    client = common.bq_client()
    out: dict = {}
    try:
        usage = job_usage(client, run_id)
    except Exception as exc:  # JOBS_BY_USER needs bigquery.jobs.list: without it the steps are zero-filled
        usage, out["usage_error"] = {}, f"{type(exc).__name__}: {exc}"[:500]
        log.error("job usage failed: %s", out["usage_error"])
    if manifest is not None and len(results) == len(STEPS) and all(r.ok for r in results):
        try:
            out |= detector_check(client, manifest)
        except Exception as exc:
            out["detector_error"] = f"{type(exc).__name__}: {exc}"[:500]
            log.error("detector check failed: %s", out["detector_error"])
    rows = metrics_rows(run_id, batch, results, usage)
    out |= {"steps": rows, "bytes_billed": sum(r["bytes_billed"] for r in rows)}
    try:
        append_runs(client, rows)
    except Exception as exc:
        exc.partial_status = out  # not `.status`: HTTP client errors often carry an int there
        raise
    return out


@cloud_event
def run_pipeline(event) -> None:
    name = event.data["name"]
    if not is_batch_manifest(name):
        return                                   # CSVs, _status.json and anything outside landing/ are ignored
    bucket = _bucket(event.data["bucket"])
    run_batch(lambda n: bucket.blob(n).download_as_bytes(),
              lambda n, b: bucket.blob(n).upload_from_string(b, content_type="application/json"),
              name, record=record, gold_max=gold_max_date)


def _cli(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backfill").add_argument("--bucket", help="default: $RAW_BUCKET")
    sub.add_parser("run").add_argument("manifest", help="gs://BUCKET/landing/<as_of>/_manifest.json")
    args = ap.parse_args(argv)
    if args.cmd == "backfill":
        print(backfill(args.bucket))
        return
    bucket_name, name = args.manifest.removeprefix("gs://").split("/", 1)
    bucket = _bucket(bucket_name)
    run_batch(lambda n: bucket.blob(n).download_as_bytes(),
              lambda n, b: bucket.blob(n).upload_from_string(b, content_type="application/json"), name,
              record=record, gold_max=gold_max_date)


if __name__ == "__main__":
    _cli()
