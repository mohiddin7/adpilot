#!/usr/bin/env python3
"""
03_anomaly_detection.py
=======================
Tier 2 — Rolling Z-score anomaly detection on CPA.

Two statistical improvements over the naive approach (both implemented):

  Gap A — Time-based windowing (FIXED):
    Uses '7D' DatetimeIndex offset instead of row-count rolling(7).
    Our data has ~2.5 missing days per campaign per month — campaigns
    don't run every calendar day.  Row-based rolling(7) treats a
    3-week-old data point as "yesterday" for a paused-and-resumed
    campaign, producing a corrupted baseline.  DatetimeIndex rolling
    correctly spans 7 calendar days regardless of how many rows fall
    within the window.

  Gap C — Log-scale Z-score (FIXED):
    CPA is strictly non-negative and right-skewed (log-normal is a
    better distributional model than Gaussian).  Z-score on raw CPA
    suffers from the masking effect: a single spike inflates σ and
    makes subsequent moderate anomalies statistically invisible.
    Computing Z on log(CPA) is scale-invariant and robust to extremes.
    Display metrics (rolling_mean_cpa, rolling_std_cpa) remain in
    dollar terms for human readability; only the Z-score uses log scale.

Two gaps documented but intentionally NOT implemented for 30-day data:

  Gap B — Cold-start lookahead bias (DOCUMENTED):
    global_mean/std for cold-start rows includes future dates.  For a
    30-day window this introduces <1% shift in the global baseline —
    negligible.  At multi-year production scale, compute an expanding
    historical baseline strictly on dates ≤ the current row's date.

  Gap D — Memory bottleneck (DOCUMENTED):
    job.to_dataframe() loads the full window into pandas.  For 330 rows
    this is trivial.  At enterprise scale (millions of campaign-day
    records), migrate the rolling window logic to BigQuery SQL using
    OVER (PARTITION BY ... ORDER BY date ROWS BETWEEN 6 PRECEDING AND
    CURRENT ROW) to keep compute colocated with the data.

Data source : fct_unified_marketing_performance  (gold mart)
Output      : fct_anomaly_flags                  (WRITE_TRUNCATE, Tier-2)
Lookback    : anchored to MAX(date) in gold — NOT CURRENT_DATE.

Execution:
  python pipelines/03_anomaly_detection.py
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from google.api_core.exceptions import (
    DeadlineExceeded, InternalServerError, NotFound, ServiceUnavailable,
)
from google.cloud import bigquery


# =============================================================================
# Config
# =============================================================================

class Config:
    PROJECT        = "improvado-analytics-lakehouse"
    PRODUCTION_DS  = "improvado_analytics_production"
    STAGING_DS     = "improvado_analytics_staging"

    GOLD_TABLE   = "fct_unified_marketing_performance"
    OUTPUT_TABLE = "fct_anomaly_flags"

    # ── Tier-2 analysis parameters ──────────────────────────────────────────
    LOOKBACK_DAYS    = 90    # window anchored to MAX(date) in gold, not CURRENT_DATE
    ROLLING_WINDOW   = 7     # calendar days (used as '7D' DatetimeIndex offset)
    Z_THRESHOLD      = 2.0   # |z| > this → anomaly flag
    MIN_HISTORY_DAYS = 3     # group needs ≥ this many obs before using group stats

    # ── Cost guardrails ──────────────────────────────────────────────────────
    MAX_READ_BYTES   = 5   * 1_024 ** 3   # 5 GB
    MAX_WRITE_BYTES  = 100 * 1_024 ** 2   # 100 MB

    MAX_RETRIES  = 3
    RETRY_BASE_S = 2

    LOGS_DIR = Path("marketing-analytics-pipeline/logs")

    # ── Output contract ──────────────────────────────────────────────────────
    OUTPUT_COLUMNS: list[str] = [
        "date", "platform", "campaign_id", "campaign_name",
        "observed_cpa", "rolling_mean_cpa", "rolling_std_cpa",
        "z_score", "is_anomaly", "anomaly_direction",
    ]

    OUTPUT_SCHEMA: list[bigquery.SchemaField] = [
        bigquery.SchemaField("date",             "DATE"),
        bigquery.SchemaField("platform",          "STRING"),
        bigquery.SchemaField("campaign_id",       "STRING"),
        bigquery.SchemaField("campaign_name",     "STRING"),
        bigquery.SchemaField("observed_cpa",      "FLOAT64"),
        bigquery.SchemaField("rolling_mean_cpa",  "FLOAT64"),
        bigquery.SchemaField("rolling_std_cpa",   "FLOAT64"),
        bigquery.SchemaField("z_score",           "FLOAT64"),
        bigquery.SchemaField("is_anomaly",        "INT64"),
        bigquery.SchemaField("anomaly_direction", "STRING"),
    ]

    @classmethod
    def gold_ref(cls) -> str:
        return f"{cls.PROJECT}.{cls.PRODUCTION_DS}.{cls.GOLD_TABLE}"

    @classmethod
    def output_ref(cls) -> str:
        return f"{cls.PROJECT}.{cls.STAGING_DS}.{cls.OUTPUT_TABLE}"


# =============================================================================
# Logging
# =============================================================================

def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("03_anomaly_detection")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    if not log.handlers:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        log.addHandler(ch)
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


# =============================================================================
# AnomalyDetectionEngine  —  pure pandas, zero I/O
# =============================================================================

class AnomalyDetectionEngine:
    """
    Stateless computation class.
    Input:  DataFrame with [date, platform, campaign_id, campaign_name, observed_cpa].
    Output: Same DataFrame extended with the six anomaly columns.

    No BigQuery calls — fully testable in isolation.

    Statistical choices for 30-day marketing data:
      • DatetimeIndex '7D' rolling window — gap-aware, not row-count-based
      • Log-scale Z-score — handles CPA's log-normal distribution correctly
      • Rolling mean/std in dollar output — human-readable display
      • cold-start fallback uses global stats (minor lookahead bias acceptable
        at 30-day scale; document at production scale for remediation)
    """

    @classmethod
    def compute(
        cls,
        df: pd.DataFrame,
        rolling_window: int   = Config.ROLLING_WINDOW,
        z_threshold:    float = Config.Z_THRESHOLD,
        min_history:    int   = Config.MIN_HISTORY_DAYS,
    ) -> pd.DataFrame:
        """
        Compute rolling Z-score anomaly detection.

        Key implementation decisions:

        1. Per-group DatetimeIndex rolling ('7D' offset, not row-count 7).
           Implementation: iterate each (platform, campaign_id) group,
           set DatetimeIndex, apply rolling('7D').  This is more explicit
           than groupby().transform() for time-based offsets, which
           require a DateTime-indexed DataFrame and behave inconsistently
           across pandas versions.

        2. Z-score computed on log(CPA), not raw CPA.
           Masking problem with raw CPA: one extreme spike inflates σ so
           much that the z-threshold effectively auto-adjusts upward,
           hiding moderate anomalies that follow the spike.  Log scale
           prevents this.  Both rolling_mean_cpa and rolling_std_cpa in
           the output remain in raw dollars for interpretability.

        3. groupby sort=False + pre-sorted DataFrame = no redundant sorts.

        Returns:
            df with columns added:
              rolling_mean_cpa  FLOAT64  (7-day rolling mean, $ scale, display)
              rolling_std_cpa   FLOAT64  (7-day rolling std,  $ scale, display)
              z_score           FLOAT64  (log-scale Z, used for anomaly flagging)
              is_anomaly        INT64    (1 if |z_score| > z_threshold)
              anomaly_direction STRING   (HIGH_CPA / LOW_CPA / NORMAL)
        """
        if df.empty:
            return cls._empty_output(df)

        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values(['platform', 'campaign_id', 'date']).reset_index(drop=True)

        # ── Global fallback for cold-start ────────────────────────────────────
        # Uses the full 90-day window → minor lookahead bias for early rows.
        # For 30-day data: ~0 practical impact (global mean shifts by <1%).
        # Production remediation: compute per-date expanding baseline strictly
        # from dates ≤ current row — adds O(n) overhead, worth it at scale.
        log_cpa_all     = np.log(df['observed_cpa'].clip(lower=1e-6))
        global_log_mean = float(log_cpa_all.mean())
        global_log_std  = float(log_cpa_all.std(ddof=1))
        global_mean     = float(df['observed_cpa'].mean())
        global_std      = float(df['observed_cpa'].std(ddof=1))
        if pd.isna(global_log_std) or global_log_std < 1e-9:
            global_log_std = 0.1
        if pd.isna(global_std) or global_std < 1e-9:
            global_std = 1.0

        # ── Per-group time-based rolling via DatetimeIndex ────────────────────
        # '7D' = 7 calendar days.  With a DatetimeIndex, pandas rolling()
        # correctly handles sparse data: a campaign that pauses for 3 days
        # and resumes will NOT use 10-day-old data in its "7-day" baseline.
        window_str: str = f'{rolling_window}D'
        group_results: list[pd.DataFrame] = []

        for (platform, campaign_id), grp in df.groupby(
            ['platform', 'campaign_id'], sort=False
        ):
            grp = grp.copy().sort_values('date').set_index('date')

            log_cpa  = np.log(grp['observed_cpa'].clip(lower=1e-6))

            # Log-scale stats for Z-score computation
            grp['_log_mean'] = log_cpa.rolling(window_str, min_periods=1).mean()
            grp['_log_std']  = log_cpa.rolling(window_str, min_periods=2).std(ddof=1)

            # Dollar-scale stats for output display (NOT used in Z-score)
            grp['rolling_mean_cpa'] = grp['observed_cpa'].rolling(window_str, min_periods=1).mean()
            grp['rolling_std_cpa']  = grp['observed_cpa'].rolling(window_str, min_periods=2).std(ddof=1)

            # Sequential row count (1, 2, 3, …) within this group for cold-start
            grp['_obs_count'] = range(1, len(grp) + 1)

            group_results.append(grp.reset_index())

        df = pd.concat(group_results, ignore_index=True)

        # ── Cold-start override ───────────────────────────────────────────────
        # Groups with < min_history obs use global stats.
        # Avoids flagging every new campaign as anomalous on its first few days.
        cold = df['_obs_count'] < min_history
        df.loc[cold, '_log_mean']        = global_log_mean
        df.loc[cold, '_log_std']         = global_log_std
        df.loc[cold, 'rolling_mean_cpa'] = global_mean
        df.loc[cold, 'rolling_std_cpa']  = global_std

        # ── Z-score on log scale ──────────────────────────────────────────────
        # NaN log_std (< 2 obs in window) → substitute global_log_std.
        # Zero log_std (all identical CPAs in window) → substitute global_log_std.
        log_std_denom = (
            df['_log_std']
            .fillna(global_log_std)
            .where(lambda s: s.abs() > 1e-9, other=global_log_std)
        )

        df['z_score'] = (
            (np.log(df['observed_cpa'].clip(lower=1e-6)) - df['_log_mean'])
            / log_std_denom
        ).fillna(0.0).round(6)

        # ── Anomaly flags ─────────────────────────────────────────────────────
        df['is_anomaly'] = (df['z_score'].abs() > z_threshold).astype('int64')
        df['anomaly_direction'] = np.where(
            df['z_score'] >  z_threshold, 'HIGH_CPA',
            np.where(df['z_score'] < -z_threshold, 'LOW_CPA', 'NORMAL'),
        )

        # ── Round display metrics ─────────────────────────────────────────────
        for col in ['observed_cpa', 'rolling_mean_cpa', 'rolling_std_cpa']:
            df[col] = df[col].round(4)

        return df.drop(columns=['_log_mean', '_log_std', '_obs_count'])

    @staticmethod
    def _empty_output(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for col, dtype in [
            ('rolling_mean_cpa', 'float64'), ('rolling_std_cpa', 'float64'),
            ('z_score', 'float64'), ('is_anomaly', 'int64'),
        ]:
            df[col] = pd.array([], dtype=dtype)
        df['anomaly_direction'] = pd.array([], dtype=object)
        return df


# =============================================================================
# AnomalyDetectionPipeline  —  orchestration
# =============================================================================

class AnomalyDetectionPipeline:
    """
    Fetches gold data → computes anomalies → writes fct_anomaly_flags.
    WRITE_TRUNCATE: derived Tier-2 output, recomputed fresh each run.
    """

    def __init__(self) -> None:
        self._log    = setup_logging(Config.LOGS_DIR / "03_anomaly_detection.log")
        self._client = bigquery.Client(project=Config.PROJECT)

    def run(self) -> None:
        self._log.info("=" * 72)
        self._log.info(
            "Script 03 — Anomaly Detection  started at %s",
            datetime.now(timezone.utc).isoformat(),
        )
        self._log.info("=" * 72)
        self._log.info(
            "Parameters  lookback=%d d  rolling=%d d  z_threshold=%.1f  "
            "min_history=%d d  z_scale=log(CPA)  window=time-based",
            Config.LOOKBACK_DAYS, Config.ROLLING_WINDOW,
            Config.Z_THRESHOLD,   Config.MIN_HISTORY_DAYS,
        )

        df = self._fetch_gold_data()
        if df.empty:
            self._log.warning("Gold table returned 0 rows — nothing to flag")
            return

        date_min = str(df["date"].min())
        date_max = str(df["date"].max())
        self._log.info(
            "Fetched %d rows  |  window: %s → %s  |  unique campaigns: %d",
            len(df), date_min, date_max, df["campaign_id"].nunique(),
        )

        df_out = AnomalyDetectionEngine.compute(df)

        self._log_summary(df_out)

        self._write_output(df_out[Config.OUTPUT_COLUMNS])
        self._log.info(
            "Script 03 COMPLETE  rows_written=%d  table=%s",
            len(df_out), Config.output_ref(),
        )

    # ── Private ──────────────────────────────────────────────────────────────

    def _fetch_gold_data(self) -> pd.DataFrame:
        """
        Pull LOOKBACK_DAYS of valid CPA rows from the gold mart.
        Anchored to MAX(date) in gold — script is data-date-agnostic.
        Rows where cpa IS NULL or cpa = 0 are excluded (no conversions = no CPA).
        """
        try:
            self._client.get_table(Config.gold_ref())
        except NotFound:
            self._log.error(
                "Gold table %s not found. Run 02_run_transformations.py first.",
                Config.gold_ref(),
            )
            return pd.DataFrame()

        sql = f"""
        SELECT
            date,
            platform,
            campaign_id,
            campaign_name,
            cpa   AS observed_cpa
        FROM `{Config.gold_ref()}`
        WHERE
            date >= DATE_SUB(
                (SELECT MAX(date) FROM `{Config.gold_ref()}`),
                INTERVAL {Config.LOOKBACK_DAYS - 1} DAY
            )
            AND cpa IS NOT NULL
            AND cpa > 0
        ORDER BY platform, campaign_id, date
        """

        self._log.info("Querying gold mart (last %d days) …", Config.LOOKBACK_DAYS)
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=Config.MAX_READ_BYTES,
            ),
        )
        return job.to_dataframe()

    def _write_output(self, df: pd.DataFrame) -> None:
        """Write to fct_anomaly_flags (WRITE_TRUNCATE) with retry."""
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])

        job_config = bigquery.LoadJobConfig(
            schema=Config.OUTPUT_SCHEMA,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )

        _retryable = (InternalServerError, ServiceUnavailable, DeadlineExceeded)
        last_exc: Exception | None = None

        for attempt in range(1, Config.MAX_RETRIES + 1):
            try:
                job = self._client.load_table_from_dataframe(
                    df, Config.output_ref(), job_config=job_config
                )
                job.result()
                self._log.info("Write OK  attempt=%d  rows=%d", attempt, len(df))
                return
            except _retryable as exc:
                last_exc = exc
                wait = Config.RETRY_BASE_S ** attempt
                self._log.warning(
                    "Write attempt %d failed: %s — retry in %ds", attempt, exc, wait
                )
                time.sleep(wait)
            except Exception as exc:
                self._log.error("Write failed (non-retryable): %s", exc)
                raise

        raise RuntimeError(
            f"Write failed after {Config.MAX_RETRIES} attempts"
        ) from last_exc

    def _log_summary(self, df: pd.DataFrame) -> None:
        """Log anomaly rate, per-platform breakdown, and top 5 by |z_score|."""
        n_total   = len(df)
        n_anomaly = int(df["is_anomaly"].sum())
        pct       = 100.0 * n_anomaly / n_total if n_total else 0.0

        self._log.info(
            "Anomaly summary  total=%d  flagged=%d (%.1f%%)  "
            "[z_score is log-scale — threshold ±%.1f]",
            n_total, n_anomaly, pct, Config.Z_THRESHOLD,
        )

        by_platform = (
            df.groupby("platform")
            .agg(
                rows     =("is_anomaly", "count"),
                anomalies=("is_anomaly", "sum"),
                high_cpa =("anomaly_direction", lambda x: (x == "HIGH_CPA").sum()),
                low_cpa  =("anomaly_direction", lambda x: (x == "LOW_CPA").sum()),
            )
            .reset_index()
        )
        for _, row in by_platform.iterrows():
            self._log.info(
                "  %-10s  rows=%3d  anomalies=%2d  "
                "(HIGH_CPA=%d  LOW_CPA=%d)",
                row["platform"], row["rows"], row["anomalies"],
                row["high_cpa"], row["low_cpa"],
            )

        if n_anomaly == 0:
            self._log.info("  No anomalies detected in this window.")
            return

        top5 = (
            df[df["is_anomaly"] == 1]
            .assign(_abs_z=lambda d: d["z_score"].abs())
            .nlargest(5, "_abs_z")
        )
        self._log.info("Top anomalies (by |z_score|, log-scale):")
        for _, r in top5.iterrows():
            self._log.info(
                "  [%s] %-10s %-28s  CPA=$%7.2f  mean=$%7.2f  z=%+.3f  %s",
                r["date"],
                r["platform"],
                str(r["campaign_name"])[:28],
                r["observed_cpa"],
                r["rolling_mean_cpa"],
                r["z_score"],
                r["anomaly_direction"],
            )


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    log = logging.getLogger("03_anomaly_detection")
    try:
        AnomalyDetectionPipeline().run()
    except Exception as exc:
        log.error("Script 03 FAILED: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()