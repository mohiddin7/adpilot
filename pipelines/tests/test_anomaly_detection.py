"""
tests/test_anomaly_detection.py
================================
Unit tests for the modified-Z anomaly detector in pipelines/03_anomaly_detection.py.

Loads the engine module via importlib because the script's filename starts
with a digit (not a valid Python identifier).  No BigQuery is touched —
all tests run against synthetic DataFrames.

Run with:  pytest tests/test_anomaly_detection.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ─── Import the digit-prefixed module ────────────────────────────────────────

_PIPELINES_DIR = Path(__file__).resolve().parent.parent
_MODULE_PATH   = _PIPELINES_DIR / "03_anomaly_detection.py"

_spec = importlib.util.spec_from_file_location("anomaly_03", _MODULE_PATH)
_mod  = importlib.util.module_from_spec(_spec)
sys.modules["anomaly_03"] = _mod
_spec.loader.exec_module(_mod)

AnomalyDetectionEngine = _mod.AnomalyDetectionEngine
Config                 = _mod.Config


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_df(
    n_days:        int,
    cpas:          list[float] | None = None,
    *,
    start:         str  = "2024-01-01",
    platform:      str  = "Facebook",
    campaign_id:   str  = "CAMP_001",
    campaign_name: str  = "Test Campaign",
    conversions:   int  = 100,
) -> pd.DataFrame:
    """
    Build a synthetic campaign-day DataFrame in the shape the engine expects.

    cpas: list of CPA values per day (length must equal n_days).  If None,
          all days get $10 CPA.  Spend is back-computed as cpa * conversions.
    """
    if cpas is None:
        cpas = [10.0] * n_days
    assert len(cpas) == n_days, "len(cpas) must equal n_days"

    dates = pd.date_range(start=start, periods=n_days, freq="D")
    return pd.DataFrame({
        "date":          dates,
        "platform":      [platform]      * n_days,
        "campaign_id":   [campaign_id]   * n_days,
        "campaign_name": [campaign_name] * n_days,
        "spend":         [cpa * conversions for cpa in cpas],
        "conversions":   [conversions]   * n_days,
    })


# ─── Test 1: insufficient history ────────────────────────────────────────────

def test_insufficient_history_returns_normal():
    """
    A campaign with 5 days of history yields severity=NORMAL,
    confidence=low, baseline_method=insufficient_history for every row
    (no row has ≥ 7 prior days available).
    """
    df = _make_df(n_days=5, cpas=[10, 12, 11, 13, 50])  # day 5 looks extreme
    out = AnomalyDetectionEngine.compute(df)

    assert len(out) == 5, "engine should emit one row per input row"
    assert (out["severity"]        == "NORMAL").all(),  "all rows must be NORMAL"
    assert (out["confidence"]      == "low").all(),     "all rows must be low confidence"
    assert (out["baseline_method"] == "insufficient_history").all()
    assert (out["is_anomaly"]      == 0).all()
    assert (out["anomaly_direction"] == "NORMAL").all()
    # Even the obvious spike on day 5 must NOT flag — not enough history yet.
    last = out.iloc[-1]
    assert last["observed_cpa"] == pytest.approx(50.0)
    assert last["severity"] == "NORMAL"


# ─── Test 2: zero MAD edge case ──────────────────────────────────────────────

def test_mad_zero_returns_normal():
    """
    When every baseline value is identical, MAD = 0 → modified_z = 0,
    severity must be NORMAL, confidence must be low (we mark stable
    campaigns as low-confidence rather than fabricating divide-by-zero
    anomalies).
    """
    # 14 identical days — every later row sees a 14-day baseline of $10s
    df = _make_df(n_days=14, cpas=[10.0] * 14)
    out = AnomalyDetectionEngine.compute(df)

    # Rows past the warm-up (index 7+) should hit the mad==0 branch.
    post_warmup = out.iloc[7:]
    assert not post_warmup.empty

    assert (post_warmup["severity"]   == "NORMAL").all()
    assert (post_warmup["confidence"] == "low").all()
    assert (post_warmup["is_anomaly"] == 0).all()

    # modified_z should be exactly 0 (not NaN) when MAD == 0.
    assert (post_warmup["modified_z_score"].fillna(-999) == 0.0).all()
    # baseline_method is the chosen method (rolling_14d or day_of_week),
    # NOT 'insufficient_history' — there are enough points; they're just flat.
    assert post_warmup["baseline_method"].isin({"rolling_14d", "day_of_week"}).all()


# ─── Test 3: outlier doesn't contaminate the next day's baseline ─────────────

def test_outlier_does_not_contaminate_next_day():
    """
    Insert one extreme value on day N.  The next day's baseline (which now
    includes the spike) should still flag the spike-day correctly while
    leaving day N+1 NORMAL — because median and MAD are robust to a single
    outlier.

    This is the key behavior that breaks under standard Z (mean + std):
    under standard Z, the spike inflates σ and day N+1 either misses real
    anomalies or shows a phantom z-score from the shifted mean.
    """
    # 13 noisy-but-stable days at ~$10, then a spike at $80, then a normal day.
    rng_cpas = [10.1, 9.8, 10.3, 9.9, 10.2, 10.0, 9.7,
                10.4, 9.9, 10.1, 9.8, 10.2, 10.0]
    spike   = 80.0
    after   = 10.0
    cpas    = rng_cpas + [spike, after]
    df      = _make_df(n_days=len(cpas), cpas=cpas)

    out = AnomalyDetectionEngine.compute(df).sort_values("date").reset_index(drop=True)
    assert len(out) == 15

    spike_row = out.iloc[13]
    next_row  = out.iloc[14]

    # Spike day: baseline is the first 13 stable days. Median≈10, MAD small,
    # observed=80 → very large positive modified_z → at least SEVERE.
    assert spike_row["observed_cpa"] == pytest.approx(spike)
    assert spike_row["severity"] in {"SEVERE", "CRITICAL"}, (
        f"expected SEVERE/CRITICAL, got {spike_row['severity']} "
        f"(mod_z={spike_row['modified_z_score']})"
    )
    assert spike_row["anomaly_direction"] == "HIGH_CPA"
    assert spike_row["is_anomaly"] == 1

    # Next day: baseline now includes the spike (13 normals + 1 spike).
    # Median is unchanged (~10) because 14 values' median is the avg of
    # ranks 7 and 8 — both fall in the cluster of $10s. MAD is unchanged
    # for the same reason. So observed=10 must NOT flag.
    assert next_row["observed_cpa"] == pytest.approx(after)
    assert next_row["severity"] == "NORMAL", (
        f"expected NORMAL on day after spike, got {next_row['severity']} "
        f"(mod_z={next_row['modified_z_score']})"
    )
    assert next_row["is_anomaly"] == 0


# ─── Test 4: day-of-week baseline preference ────────────────────────────────

def test_day_of_week_baseline_used_when_available():
    """
    Once a campaign has ≥ 4 same-DOW prior observations within the 14-day
    window, baseline_method should switch to 'day_of_week'.

    14-day window per row, so to get 4 prior same-DOW rows we need the
    campaign to have run for at least 4 * 7 = 28 days.  Use 35 days to be
    safe and check the final row.
    """
    n = 35
    df = _make_df(n_days=n, cpas=[10.0 + 0.1 * i for i in range(n)])  # mild trend
    out = AnomalyDetectionEngine.compute(df).sort_values("date").reset_index(drop=True)

    # The final row should have a 'day_of_week' baseline.
    last = out.iloc[-1]
    assert last["baseline_method"] == "day_of_week", (
        f"expected day_of_week on day {n}, got {last['baseline_method']}"
    )
    # And at least some baseline_size > 0 with DOW chosen
    assert last["baseline_size"] >= Config.MIN_DOW_BASELINE_SIZE

    # Conversely, an early row should NOT use day_of_week (not enough DOW
    # samples in its 14-day lookback).  Day index 8 (zero-based) has only
    # 1 same-DOW prior sample — insufficient — so it must use rolling_14d.
    early = out.iloc[8]
    assert early["baseline_method"] in {"rolling_14d", "insufficient_history"}


# ─── Test 5: severity bucket boundaries ──────────────────────────────────────

def test_severity_buckets_at_thresholds():
    """
    Construct a baseline with known median and MAD, then inject target CPAs
    that produce specific modified_z values, and verify the severity tier.

    Baseline = the 7 values [1, 2, 3, 4, 5, 6, 7].
      median  = 4
      MAD     = median(|x - 4|) = median([3, 2, 1, 0, 1, 2, 3]) = 2
      0.6745 * (observed - 4) / 2 = modified_z
        ⇒ observed = 4 + (modified_z * 2 / 0.6745)

    Test points (choosing observed values that give |mod_z| just above
    each threshold, so we land cleanly inside each bucket):

      |mod_z| = 3.6 → observed ≈ 4 + 10.674 ≈ 14.674  → MODERATE
      |mod_z| = 5.0 → observed ≈ 4 + 14.825 ≈ 18.825  → SEVERE
      |mod_z| = 6.5 → observed ≈ 4 + 19.273 ≈ 23.273  → CRITICAL
      |mod_z| = 1.0 → observed ≈ 4 +  2.965 ≈  6.965  → NORMAL
    """
    base_cpas = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]

    def _observed_for(target_z: float) -> float:
        # invert: observed = median + target_z * MAD / mad_constant
        return 4.0 + target_z * 2.0 / Config.MAD_CONSTANT

    cases = [
        (1.0, "NORMAL"),
        (3.6, "MODERATE"),
        (5.0, "SEVERE"),
        (6.5, "CRITICAL"),
    ]

    for target_z, expected_severity in cases:
        observed = _observed_for(target_z)
        cpas = base_cpas + [observed]  # 8 days; day 8's baseline is days 1-7
        df = _make_df(n_days=8, cpas=cpas)
        out = AnomalyDetectionEngine.compute(df).sort_values("date").reset_index(drop=True)

        row = out.iloc[-1]
        assert row["baseline_size"] == 7, "baseline should be exactly 7 prior days"
        assert row["baseline_method"] == "rolling_14d"
        # modified_z should land near target_z (within a small numeric tolerance)
        assert row["modified_z_score"] == pytest.approx(target_z, abs=1e-3), (
            f"expected mod_z={target_z}, got {row['modified_z_score']}"
        )
        assert row["severity"] == expected_severity, (
            f"for mod_z={target_z}: expected {expected_severity}, "
            f"got {row['severity']}"
        )


# ─── Bonus: empty input doesn't crash ────────────────────────────────────────

def test_empty_dataframe_returns_empty_output():
    """The engine must not raise on an empty input DataFrame."""
    empty = pd.DataFrame(columns=[
        "date", "platform", "campaign_id", "campaign_name",
        "spend", "conversions",
    ])
    out = AnomalyDetectionEngine.compute(empty)
    assert len(out) == 0
    # All output columns should still be present (for downstream schema safety).
    assert list(out.columns) == Config.OUTPUT_COLUMNS


# ─── Bonus: conversions==0 rows are dropped ──────────────────────────────────

def test_zero_conversions_rows_are_dropped():
    """
    Rows with conversions == 0 have undefined CPA and must be absent from
    the output entirely (not written as NORMAL).
    """
    df = _make_df(n_days=10)
    df.loc[3, "conversions"] = 0  # day 4 has zero conversions
    out = AnomalyDetectionEngine.compute(df)
    # Output should have 9 rows (one row dropped).
    assert len(out) == 9
    # The dropped date must not appear in the output.
    dropped_date = df.loc[3, "date"].date()
    assert (out["date"] != dropped_date).all()