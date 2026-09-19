import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402

gen = common.load_script("00_generate_synthetic")


def test_retail_events_land_on_each_years_calendar():
    ev = gen.retail_events(2016)
    assert ev["black_friday"] == (date(2016, 11, 25),) and ev["cyber_monday"] == (date(2016, 11, 28),)
    ev = gen.retail_events(2026)
    assert ev["black_friday"] == (date(2026, 11, 27),) and ev["cyber_monday"] == (date(2026, 11, 30),)
    assert all(d.weekday() < 5 and 8 <= d.day <= 16 for d in ev["shipping_peak"])


def test_derive_seasonality_recovers_a_planted_shape():
    days = pd.date_range("2016-08-01", "2017-08-01")
    tx = np.where(days.dayofweek >= 5, 50.0, 100.0)
    tx[days == "2016-11-28"] *= 3                          # a Cyber Monday spike
    cal = gen.derive_seasonality(pd.DataFrame({"date": days, "transactions": tx}))
    assert len(cal["doy"]) == 366 and len(cal["dow"]) == 7 and set(cal["events"]) == set(gen.EVENTS)
    assert cal["dow"][5] < 0.8 < 1.0 < cal["dow"][0]
    assert cal["events"]["cyber_monday"] > 2.0
    assert 0.9 < cal["events"]["christmas"] < 1.1          # nothing planted there


def test_committed_seasonality_matches_the_measured_ga_shape():
    cal = json.loads(gen.SEASONALITY_PATH.read_text())
    assert cal["source"] == gen.SEASONALITY_SQL
    assert len(cal["doy"]) == 366 and len(cal["dow"]) == 7 and set(cal["events"]) == set(gen.EVENTS)
    assert all(0.05 < v < 10 for v in cal["doy"] + cal["dow"] + list(cal["events"].values()))
    # Measured 2026-09-23 on the GA sample: dow≈[1.22,1.20,1.19,1.16,1.15,0.50,0.58], Cyber Monday 1.64,
    # Black Friday 0.78 (a merch store), December peak ≈2.0, mid-January ≈0.63.
    assert max(cal["dow"][5:]) < 0.7 < 1.1 < min(cal["dow"][:5])
    assert cal["events"]["cyber_monday"] > 1.4 and cal["events"]["black_friday"] < 1.0
    assert max(cal["doy"][330:360]) > 1.5 and cal["doy"][14] < 0.8


def test_committed_levels_match_data_raw():
    # The functions deploy from pipelines/ and cannot read data/raw: levels.json must never drift from it.
    assert json.loads(gen.LEVELS_PATH.read_text()) == gen.derive_levels()


def test_levels_cover_every_real_campaign_and_header():
    levels = gen.derive_levels()
    for platform, spec in gen.PLATFORMS.items():
        raw = pd.read_csv(gen.RAW_DIR / spec["csv"])
        assert levels[platform]["header"] == list(raw.columns)
        assert {c["campaign_id"] for c in levels[platform]["campaigns"]} == set(raw.campaign_id)
