import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402

main = common.load_script("main")
TODAY = date(2026, 9, 24)
MANIFEST = "landing/2026-09-24/_manifest.json"
NOW = datetime(2026, 9, 24, 2, 0, 1, tzinfo=timezone.utc)


class Store(dict):
    def write(self, name, body):
        self[name] = body


def _store():
    files = main.build_batch(date(2026, 9, 21), date(2026, 9, 23), TODAY, today=TODAY)
    return Store({f"landing/2026-09-24/{n}": b for n, b in files.items()})


def _status(store):
    return json.loads(store["landing/2026-09-24/_status.json"])


@pytest.mark.parametrize("name, ok", [
    ("landing/2026-09-24/_manifest.json", True),
    ("landing/2026-09-24/_status.json", False),
    ("landing/2026-09-24/google_ads.csv", False),
    ("archive/2026-09-24/_manifest.json", False),
    ("landing/_manifest.json", False),
    ("landing/a/b/_manifest.json", False),
])
def test_is_batch_manifest(name, ok):
    assert main.is_batch_manifest(name) is ok


def test_default_steps_are_the_pipeline_order():
    assert main.STEP_NAMES == ("01_ingest", "02_transform", "03_anomalies", "04_budget", "05_forecast", "07_qa")
    assert tuple(n for n, _ in main.STEPS) == main.STEP_NAMES


def test_runs_every_step_in_order_with_its_label_and_writes_ok_status():
    store, seen = _store(), []

    def step(batch_dir, manifest):
        seen.append((os.environ["PIPELINE_STEP"], os.environ["PIPELINE_RUN_ID"], sorted(p.name for p in batch_dir.iterdir())))
        return 1

    main.run_batch(store.__getitem__, store.write, MANIFEST, steps=[(n, step) for n in main.STEP_NAMES], now=NOW)
    assert [s[0] for s in seen] == list(main.STEP_NAMES)
    assert {s[1] for s in seen} == {"r20260924t020001-2026-09-24"}
    assert seen[0][2] == ["facebook_ads.csv", "google_ads.csv", "tiktok_ads.csv"]
    status = _status(store)
    assert status["ok"] and status["run_id"] == "r20260924t020001-2026-09-24" and len(status["steps"]) == 6


def test_failing_step_stops_the_run_records_it_and_reraises():
    store, called, recorded = _store(), [], {}

    def ok(batch_dir, manifest):
        called.append(os.environ["PIPELINE_STEP"])
        return 0

    def boom(batch_dir, manifest):
        raise ValueError("boom")

    def rec(run_id, batch, manifest, results):
        recorded["results"] = results
        return {}

    steps = [("01_ingest", ok), ("02_transform", ok), ("03_anomalies", boom), ("04_budget", ok)]
    with pytest.raises(ValueError, match="boom"):
        main.run_batch(store.__getitem__, store.write, MANIFEST, steps=steps, record=rec, now=NOW)
    assert called == ["01_ingest", "02_transform"]
    status = _status(store)
    assert not status["ok"] and "boom" in status["error"] and [s["ok"] for s in status["steps"]] == [True, True, False]
    assert [r.step for r in recorded["results"]] == ["01_ingest", "02_transform", "03_anomalies"]


def test_tampered_batch_runs_no_step():
    store = _store()
    store["landing/2026-09-24/google_ads.csv"] += b"2026-09-23,g_1,x,gad_1,y,1,1,1,1,1,1,1,1,1\n"
    with pytest.raises(RuntimeError, match="sha256"):
        main.run_batch(store.__getitem__, store.write, MANIFEST, steps=[("01_ingest", pytest.fail)], now=NOW)
    assert not _status(store)["ok"]


def test_metrics_failure_does_not_mask_a_good_run():
    store = _store()

    def broken(*a):
        raise RuntimeError("bq down")

    main.run_batch(store.__getitem__, store.write, MANIFEST, steps=[("01_ingest", lambda d, m: 0)], record=broken, now=NOW)
    status = _status(store)
    assert status["ok"] and "bq down" in status["metrics_error"]


def test_metrics_rows_zero_fill_steps_without_jobs():
    r = main.StepResult("01_ingest", "2026-09-24T02:00:01+00:00", 1.5, True, 10)
    rows = main.metrics_rows("r1", "landing/2026-09-24/", [r], {})
    assert rows == [{"run_id": "r1", "batch": "landing/2026-09-24/", "step": "01_ingest", "started_at": "2026-09-24T02:00:01+00:00",
                     "seconds": 1.5, "ok": True, "rows": 10, "error": "", "jobs": 0, "bytes_billed": 0, "slot_ms": 0}]


def test_detector_window_daily_and_backfill():
    assert main.detector_window({"start": "2026-09-21", "end": "2026-09-23"}) == (date(2026, 9, 21), date(2026, 9, 21))
    assert main.detector_window({"start": "2024-01-31", "end": "2026-09-23"}) == (date(2026, 6, 24), date(2026, 9, 21))
