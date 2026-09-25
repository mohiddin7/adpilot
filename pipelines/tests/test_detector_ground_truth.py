"""The anomaly detector against the generator's planted anomalies. The pins were measured once; a failure is a
detector or generator regression, never a reason to move them."""
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402

gen = common.load_script("00_generate_synthetic")
engine = common.load_script("03_anomaly_detection").AnomalyDetectionEngine
START, END, AS_OF, TODAY = date(2024, 2, 1), date(2024, 12, 31), date(2025, 1, 10), date(2026, 9, 24)
RECALL_FLOOR = 0.66   # measured 0.717 on 2026-09-21, minus 0.05, floored to the nearest 0.01
FP_CEILING = 0.128    # measured 0.1172 on 2026-09-21, plus 0.01, ceiled to the nearest 0.001


@lru_cache(maxsize=1)
def _scored():
    frames = gen.generate(START, END, AS_OF, today=TODAY)
    gold = pd.concat([f.rename(columns={"cost": "spend"}).assign(platform=gen.PLATFORMS[p]["label"]) for p, f in frames.items()])
    daily = gold.groupby(["date", "platform", "campaign_id", "campaign_name"], as_index=False)[["spend", "conversions"]].sum()
    daily["date"] = pd.to_datetime(daily["date"])
    out = engine.compute(daily)
    out["date"] = pd.to_datetime(out["date"])
    truth = gen.planted(START, END, today=TODAY).assign(date=lambda t: pd.to_datetime(t["date"]))
    return out, truth


def test_enough_plants_for_the_numbers_to_mean_something():
    _, truth = _scored()
    assert len(truth) >= 40 and set(truth.direction) == {"HIGH_CPA", "LOW_CPA"}


def test_detector_finds_planted_anomalies():
    out, truth = _scored()
    hits = truth.merge(out[out.is_anomaly == 1], on=["date", "campaign_id"])
    recall = (hits.direction == hits.anomaly_direction).sum() / len(truth)
    assert recall >= RECALL_FLOOR, f"recall {recall:.3f} < {RECALL_FLOOR}"


def test_detector_false_positive_rate():
    out, truth = _scored()
    keys = set(zip(truth.date, truth.campaign_id))
    clean = out[[k not in keys for k in zip(out.date, out.campaign_id)]]
    fp = (clean.is_anomaly == 1).mean()
    assert fp <= FP_CEILING, f"false-positive rate {fp:.4f} > {FP_CEILING}"


def test_planted_matches_generated_rows():
    # A label exists only where the generator actually applied it: same date and campaign in the output rows.
    frames = gen.generate(START, END, AS_OF, today=TODAY)
    rows = {(r.date, r.campaign_id) for f in frames.values() for r in f[["date", "campaign_id"]].itertuples()}
    truth = gen.planted(START, END, today=TODAY)
    assert all((d.isoformat(), c) in rows for d, c in zip(truth.date, truth.campaign_id))
