#!/usr/bin/env python3
"""
03_anomaly_detection.py
=======================
Tier 2 — Modified Z-score anomaly detection on CPA.

Algorithm: MAD-based modified Z-score with day-of-week-aware baseline.
See documentation/ANOMALY_DETECTION_REFACTOR_SPEC.md for full design notes.

Why this algorithm (vs. the previous rolling standard Z-score):

  1. Modified Z (median + MAD) replaces standard Z (mean + std).
     CPA distributions are right-skewed and contain real outliers.
     Mean/std are pulled by those outliers, masking subsequent moderate
     anomalies (one bad day inflates std → next day's z stays small even
     when the underlying CPA is genuinely off).  Median and MAD are
     robust: a single extreme value cannot shift them.

  2. Day-of-week-aware baseline (when ≥4 same-DOW prior samples exist).
     Marketing data has weekly seasonality.  Monday CPAs ≠ Saturday CPAs.
     A rolling 7- or 14-day baseline averages across DOWs and treats
     naturally low Mondays as anomalous and naturally high Sundays as
     normal.  Same-DOW baseline removes that bias.

  3. Hard warm-up gate (≥7 baseline points required).
     The previous 3-day fallback emitted z-scores on days 1–3 that were
     essentially noise.  Now: < 7 prior points → severity=NORMAL,
     confidence=low, baseline_method='insufficient_history'.  No false
     anomalies on new campaigns.

  4. Four-tier severity (NORMAL / MODERATE / SEVERE / CRITICAL).
     Downstream consumers can distinguish "worth a glance" from
     "act this week" rather than treating every z=2.1 the same as z=6.

  5. Confidence label (high / medium / low).
     Dashboards can filter to high-confidence flags by default and
     surface medium/low only on demand.

Backward compatibility:
  Existing columns `rolling_mean_cpa`, `rolling_std_cpa`, `z_score` are
  retained with the same names but now carry baseline median, baseline
  MAD, and modified Z respectively.  `is_anomaly = 1` iff
  `severity != NORMAL`.  New columns: `modified_z_score`, `severity`,
  `baseline_method`, `baseline_size`, `confidence`, `days_of_history`.

Data source : fct_unified_marketing_performance  (gold mart)
Output      : fct_anomaly_flags                  (WRITE_TRUNCATE, Tier-2)
Lookback    : anchored to the mature cutoff, MAX(date) − RESTATING_DAYS in gold — NOT CURRENT_DATE.

Execution:
  python pipelines/03_anomaly_detection.py
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from google.api_core.exceptions import (
    DeadlineExceeded, InternalServerError, NotFound, ServiceUnavailable,
)
from google.cloud import bigquery

import common


# =============================================================================
# Config
# =============================================================================

class Config:
    PROJECT        = common.PROJECT
    PRODUCTION_DS  = common.PRODUCTION_DS
    STAGING_DS     = common.STAGING_DS

    GOLD_TABLE   = "fct_unified_marketing_performance"
    OUTPUT_TABLE = "fct_anomaly_flags"

    # ── Tier-2 analysis parameters ──────────────────────────────────────────
    LOOKBACK_WINDOW_DAYS  = 90    # how far back to compute anomalies for
    BASELINE_WINDOW_DAYS  = 28    # per-row baseline lookback (see note below)
    MIN_BASELINE_SIZE     = 7     # min points required to emit any flag
    MIN_DOW_BASELINE_SIZE = 4     # min same-DOW points to prefer DOW baseline

    # NOTE on BASELINE_WINDOW_DAYS = 28 (deviation from spec's literal 14):
    # The refactor spec's skeleton sets baseline_window_days=14 alongside
    # min_dow_baseline_size=4 — but in a 14-day window the same-DOW subset
    # contains at most 2 samples (each weekday occurs twice in 14 days), so
    # the DOW threshold of 4 would be mathematically unreachable and the
    # day-of-week branch would be dead code.  The spec's narrative resolves
    # the ambiguity: "A campaign typically has at least 4 same-day-of-week
    # samples after 28 days" and the smoke test in §5.2 expects DOW baselines
    # "when same-DOW count ≥ 4".  28 days is the smallest window that lets
    # min_dow_baseline_size=4 actually trigger, so we use it here.  The
    # baseline_method string contract ("rolling_14d") is kept verbatim to
    # match the spec's output schema; a follow-up PR may rename it to
    # "rolling" along with the column renames mentioned in spec §3.

    # |modified_z| thresholds for severity bucketing.
    # Values map to standard-normal sigma intuition via the 0.6745 constant
    # (|mod_z| > 3.5 ≈ outside 99.95th percentile under normality).
    SEVERITY_THRESHOLDS: dict[str, float] = {
        "MODERATE": 3.5,
        "SEVERE":   4.5,
        "CRITICAL": 6.0,
    }
    MAD_CONSTANT = 0.6745         # calibrates MAD-based modified Z

    # ── Cost guardrails ──────────────────────────────────────────────────────
    MAX_READ_BYTES   = 5   * 1_024 ** 3   # 5 GB
    MAX_WRITE_BYTES  = 100 * 1_024 ** 2   # 100 MB

    MAX_RETRIES  = 3
    RETRY_BASE_S = 2

    LOGS_DIR = Path(__file__).resolve().parent / "logs"

    # ── Output contract ──────────────────────────────────────────────────────
    OUTPUT_COLUMNS: list[str] = [
        # Existing columns (semantics changed but names kept for compat)
        "date", "platform", "campaign_id", "campaign_name",
        "observed_cpa",
        "rolling_mean_cpa", "rolling_std_cpa", "z_score",
        "is_anomaly", "anomaly_direction",
        # New columns
        "modified_z_score", "severity",
        "baseline_method", "baseline_size",
        "confidence", "days_of_history",
    ]

    OUTPUT_SCHEMA: list[bigquery.SchemaField] = [
        bigquery.SchemaField("date",             "DATE",    mode="REQUIRED"),
        bigquery.SchemaField("platform",          "STRING",  mode="REQUIRED"),
        bigquery.SchemaField("campaign_id",       "STRING",  mode="REQUIRED"),
        bigquery.SchemaField("campaign_name",     "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("observed_cpa",      "FLOAT64", mode="NULLABLE"),
        # Existing columns — semantics changed but names kept for backward compat
        bigquery.SchemaField("rolling_mean_cpa",  "FLOAT64", mode="NULLABLE",
                              description="Baseline median CPA (was rolling mean)"),
        bigquery.SchemaField("rolling_std_cpa",   "FLOAT64", mode="NULLABLE",
                              description="Baseline MAD (was rolling std)"),
        bigquery.SchemaField("z_score",           "FLOAT64", mode="NULLABLE",
                              description="Modified Z-score (was standard Z)"),
        bigquery.SchemaField("is_anomaly",        "INT64",   mode="REQUIRED"),
        bigquery.SchemaField("anomaly_direction", "STRING",  mode="REQUIRED"),
        # New columns
        bigquery.SchemaField("modified_z_score",  "FLOAT64", mode="NULLABLE"),
        bigquery.SchemaField("severity",          "STRING",  mode="REQUIRED"),
        bigquery.SchemaField("baseline_method",   "STRING",  mode="REQUIRED"),
        bigquery.SchemaField("baseline_size",     "INT64",   mode="REQUIRED"),
        bigquery.SchemaField("confidence",        "STRING",  mode="REQUIRED"),
        bigquery.SchemaField("days_of_history",   "INT64",   mode="REQUIRED"),
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
    return common.get_logger(log_file.stem)


# =============================================================================
# AnomalyDetectionEngine  —  pure pandas / numpy, zero I/O
# =============================================================================

class AnomalyDetectionEngine:
    """
    Stateless computation class.
    Input:  DataFrame with [date, platform, campaign_id, campaign_name,
            spend, conversions].
    Output: DataFrame matching Config.OUTPUT_COLUMNS.

    No BigQuery calls — fully unit-testable in isolation.

    Algorithm (one row at a time, per (platform, campaign_id) group):
      1. baseline_window = prior BASELINE_WINDOW_DAYS days for this campaign,
         strictly before the target date.
      2. dow_subset = baseline_window filtered to same day-of-week as target.
      3. If |dow_subset| ≥ MIN_DOW_BASELINE_SIZE: use it (method='day_of_week').
         Else: use full baseline_window (method='rolling_14d').
      4. If |baseline| < MIN_BASELINE_SIZE: emit NORMAL/low/insufficient_history.
      5. Else: compute median, MAD.  If MAD == 0: emit NORMAL/low (stable).
      6. Else: mod_z = 0.6745 * (observed - median) / MAD.
         Bucket into NORMAL/MODERATE/SEVERE/CRITICAL by |mod_z| thresholds.
    """

    # ── Public entry point ───────────────────────────────────────────────────

    @classmethod
    def compute(
        cls,
        df: pd.DataFrame,
        baseline_window_days:  int = Config.BASELINE_WINDOW_DAYS,
        min_baseline_size:     int = Config.MIN_BASELINE_SIZE,
        min_dow_baseline_size: int = Config.MIN_DOW_BASELINE_SIZE,
        severity_thresholds:   dict[str, float] | None = None,
        mad_constant:          float = Config.MAD_CONSTANT,
    ) -> pd.DataFrame:
        """
        Compute modified-Z anomaly flags.

        Args:
            df: DataFrame with columns [date, platform, campaign_id,
                campaign_name, spend, conversions].
            baseline_window_days: lookback window for baseline (days).
            min_baseline_size: minimum baseline points to emit a non-NORMAL flag.
            min_dow_baseline_size: minimum same-DOW points to prefer DOW baseline.
            severity_thresholds: dict of |mod_z| cutoffs for MODERATE/SEVERE/CRITICAL.
            mad_constant: scaling constant (0.6745 standard for MAD-based Z).

        Returns:
            DataFrame with all Config.OUTPUT_COLUMNS populated.

        Rows where conversions == 0 or spend / conversions is NULL are
        dropped (no CPA defined) and absent from the output.
        """
        if severity_thresholds is None:
            severity_thresholds = Config.SEVERITY_THRESHOLDS

        if df.empty:
            return cls._empty_output()

        df = df.copy()

        # Compute CPA. Rows with conversions == 0 → CPA = NaN → dropped below.
        # We use .where() rather than division to avoid div-by-zero warnings.
        safe_conv = df["conversions"].where(df["conversions"] > 0)
        df["observed_cpa"] = df["spend"] / safe_conv
        df = df.dropna(subset=["observed_cpa"]).reset_index(drop=True)

        if df.empty:
            return cls._empty_output()

        # Normalize date dtype and add day-of-week for baseline selection.
        df["date"] = pd.to_datetime(df["date"])
        df["_dow"] = df["date"].dt.dayofweek  # Mon=0 … Sun=6

        df = df.sort_values(["platform", "campaign_id", "date"]).reset_index(drop=True)

        out_rows: list[dict[str, Any]] = []
        for (platform, campaign_id), grp in df.groupby(
            ["platform", "campaign_id"], sort=False
        ):
            grp = grp.sort_values("date").reset_index(drop=True)
            for _, target in grp.iterrows():
                out_rows.append(cls._compute_row(
                    target           = target,
                    group            = grp,
                    platform         = platform,
                    campaign_id      = campaign_id,
                    baseline_window_days  = baseline_window_days,
                    min_baseline_size     = min_baseline_size,
                    min_dow_baseline_size = min_dow_baseline_size,
                    severity_thresholds   = severity_thresholds,
                    mad_constant          = mad_constant,
                ))

        result = pd.DataFrame(out_rows, columns=Config.OUTPUT_COLUMNS)
        return result

    # ── Per-row computation ──────────────────────────────────────────────────

    @classmethod
    def _compute_row(
        cls,
        *,
        target: pd.Series,
        group: pd.DataFrame,
        platform: str,
        campaign_id: str,
        baseline_window_days: int,
        min_baseline_size: int,
        min_dow_baseline_size: int,
        severity_thresholds: dict[str, float],
        mad_constant: float,
    ) -> dict[str, Any]:
        """Compute one output row for one target observation."""
        target_date = target["date"]
        cutoff_lo   = target_date - pd.Timedelta(days=baseline_window_days)

        # Baseline window: strictly prior dates within lookback range.
        baseline_window = group[
            (group["date"] < target_date) & (group["date"] >= cutoff_lo)
        ]
        days_of_history = len(baseline_window)

        # Baseline selection.
        #
        # Note on gating: the refactor spec's pseudo-code applies the
        # min_baseline_size=7 check uniformly after baseline selection,
        # which would reject a day_of_week baseline with 4 same-DOW samples
        # (4 < 7).  But the spec's own confidence rules in the same section
        # treat day_of_week with 4–5 samples as a valid *medium*-confidence
        # flag — meaning the author must have intended min_baseline_size to
        # gate only the rolling path.  We resolve in favor of the confidence
        # rules: day_of_week gates on min_dow_baseline_size only;
        # rolling_14d gates on min_baseline_size; insufficient_history is
        # emitted only when neither is satisfied.
        dow_subset = baseline_window[baseline_window["_dow"] == target["_dow"]]

        if len(dow_subset) >= min_dow_baseline_size:
            baseline = dow_subset
            method   = "day_of_week"
        elif len(baseline_window) >= min_baseline_size:
            baseline = baseline_window
            method   = "rolling_14d"
        else:
            return cls._make_row(
                target=target, platform=platform, campaign_id=campaign_id,
                median=None, mad=None, mod_z=None,
                severity="NORMAL", direction="NORMAL",
                method="insufficient_history",
                baseline_size=len(baseline_window),
                days_of_history=days_of_history,
                confidence="low",
            )

        obs    = baseline["observed_cpa"].to_numpy()
        median = float(np.median(obs))
        mad    = float(np.median(np.abs(obs - median)))

        # Gate 2: zero variance → can't compute Z, emit stable/NORMAL.
        if mad == 0.0:
            return cls._make_row(
                target=target, platform=platform, campaign_id=campaign_id,
                median=median, mad=mad, mod_z=0.0,
                severity="NORMAL", direction="NORMAL",
                method=method,
                baseline_size=len(baseline),
                days_of_history=days_of_history,
                confidence="low",
            )

        mod_z      = mad_constant * (float(target["observed_cpa"]) - median) / mad
        severity   = cls._severity_for(abs(mod_z), severity_thresholds)
        direction  = cls._direction_for(severity, mod_z)
        confidence = cls._confidence_for(method, len(baseline))

        return cls._make_row(
            target=target, platform=platform, campaign_id=campaign_id,
            median=median, mad=mad, mod_z=mod_z,
            severity=severity, direction=direction,
            method=method,
            baseline_size=len(baseline),
            days_of_history=days_of_history,
            confidence=confidence,
        )

    # ── Helpers: severity / direction / confidence ───────────────────────────

    @staticmethod
    def _severity_for(abs_z: float, thresholds: dict[str, float]) -> str:
        if abs_z >= thresholds["CRITICAL"]:
            return "CRITICAL"
        if abs_z >= thresholds["SEVERE"]:
            return "SEVERE"
        if abs_z >= thresholds["MODERATE"]:
            return "MODERATE"
        return "NORMAL"

    @staticmethod
    def _direction_for(severity: str, mod_z: float) -> str:
        if severity == "NORMAL":
            return "NORMAL"
        return "HIGH_CPA" if mod_z > 0 else "LOW_CPA"

    @staticmethod
    def _confidence_for(method: str, baseline_size: int) -> str:
        if method == "insufficient_history":
            return "low"
        if method == "rolling_14d" and baseline_size < 10:
            return "medium"
        if method == "day_of_week" and baseline_size < 6:
            return "medium"
        return "high"

    # ── Row builder ──────────────────────────────────────────────────────────

    @staticmethod
    def _make_row(
        *,
        target: pd.Series,
        platform: str,
        campaign_id: str,
        median: float | None,
        mad: float | None,
        mod_z: float | None,
        severity: str,
        direction: str,
        method: str,
        baseline_size: int,
        days_of_history: int,
        confidence: str,
    ) -> dict[str, Any]:
        """Assemble one output row matching Config.OUTPUT_COLUMNS."""
        observed_cpa = round(float(target["observed_cpa"]), 4)
        median_out   = None if median is None else round(median, 4)
        mad_out      = None if mad    is None else round(mad,    4)
        mod_z_out    = None if mod_z  is None else round(mod_z,  6)

        return {
            "date":               target["date"].date(),
            "platform":           platform,
            "campaign_id":        campaign_id,
            "campaign_name":      target.get("campaign_name"),
            "observed_cpa":       observed_cpa,
            # Repurposed legacy columns (median / MAD / modified Z):
            "rolling_mean_cpa":   median_out,
            "rolling_std_cpa":    mad_out,
            "z_score":            mod_z_out,
            # Backward-compat binary flag:
            "is_anomaly":         0 if severity == "NORMAL" else 1,
            "anomaly_direction":  direction,
            # New explicit columns:
            "modified_z_score":   mod_z_out,
            "severity":           severity,
            "baseline_method":    method,
            "baseline_size":      int(baseline_size),
            "confidence":         confidence,
            "days_of_history":    int(days_of_history),
        }

    # ── Empty input fallback ─────────────────────────────────────────────────

    @staticmethod
    def _empty_output() -> pd.DataFrame:
        return pd.DataFrame({col: pd.Series(dtype="object")
                              for col in Config.OUTPUT_COLUMNS})


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
        self._client = common.bq_client()

    def run(self) -> None:
        self._log.info("=" * 72)
        self._log.info(
            "Script 03 — Anomaly Detection (modified-Z + MAD)  started at %s",
            datetime.now(timezone.utc).isoformat(),
        )
        self._log.info("=" * 72)
        self._log.info(
            "Params: lookback=%dd  baseline_window=%dd  min_baseline=%d  "
            "min_dow=%d  severity={MOD=%.1f, SEV=%.1f, CRIT=%.1f}",
            Config.LOOKBACK_WINDOW_DAYS, Config.BASELINE_WINDOW_DAYS,
            Config.MIN_BASELINE_SIZE,    Config.MIN_DOW_BASELINE_SIZE,
            Config.SEVERITY_THRESHOLDS["MODERATE"],
            Config.SEVERITY_THRESHOLDS["SEVERE"],
            Config.SEVERITY_THRESHOLDS["CRITICAL"],
        )

        df = self._fetch_gold_data()
        if df.empty:
            self._log.warning("Gold table returned 0 rows — nothing to flag")
            return

        date_min = str(df["date"].min())
        date_max = str(df["date"].max())
        self._log.info(
            "Fetched %d (date, platform, campaign) rows  |  %s → %s  |  "
            "%d unique campaigns",
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
        Pull LOOKBACK_WINDOW_DAYS of campaign-day spend/conversions from the
        gold mart.  Aggregates to (date, platform, campaign_id, campaign_name)
        because the gold grain includes sub_group_id — we want CPA at the
        campaign level (SUM(spend) / SUM(conversions)), not per ad set.

        Anchored to the newest mature date (MAX(date) − RESTATING_DAYS) so the script is data-date-agnostic.
        Rows with zero conversions are filtered out in the engine, not here,
        so the engine has visibility into a campaign's full history.
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
        WITH bounds AS (
            SELECT DATE_SUB(MAX(date), INTERVAL {common.RESTATING_DAYS} DAY) AS max_d
            FROM `{Config.gold_ref()}`
        )
        SELECT
            date,
            platform,
            campaign_id,
            campaign_name,
            SUM(spend)       AS spend,
            SUM(conversions) AS conversions
        FROM `{Config.gold_ref()}`
        CROSS JOIN bounds
        WHERE date BETWEEN DATE_SUB(bounds.max_d, INTERVAL {Config.LOOKBACK_WINDOW_DAYS - 1} DAY)
                       AND bounds.max_d
        GROUP BY date, platform, campaign_id, campaign_name
        ORDER BY platform, campaign_id, date
        """

        self._log.info("Querying gold mart (last %d days) …",
                       Config.LOOKBACK_WINDOW_DAYS)
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
        # Ensure pandas-gbq can convert the date column to BQ DATE.
        df["date"] = pd.to_datetime(df["date"])

        job_config = common.parquet_load_config(
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
                    "Write attempt %d failed: %s — retry in %ds",
                    attempt, exc, wait,
                )
                time.sleep(wait)
            except Exception as exc:
                self._log.error("Write failed (non-retryable): %s", exc)
                raise

        raise RuntimeError(
            f"Write failed after {Config.MAX_RETRIES} attempts"
        ) from last_exc

    def _log_summary(self, df: pd.DataFrame) -> None:
        """Log severity / method / confidence breakdowns plus top anomalies."""
        n_total   = len(df)
        n_anomaly = int((df["severity"] != "NORMAL").sum())
        pct       = 100.0 * n_anomaly / n_total if n_total else 0.0

        self._log.info(
            "Output summary  rows=%d  flagged=%d (%.1f%%)",
            n_total, n_anomaly, pct,
        )

        # Severity distribution
        sev_counts = df["severity"].value_counts().to_dict()
        for sev in ("NORMAL", "MODERATE", "SEVERE", "CRITICAL"):
            self._log.info("  severity=%-9s  %4d", sev, sev_counts.get(sev, 0))

        # Baseline method distribution
        for method, count in df["baseline_method"].value_counts().items():
            self._log.info("  method=%-22s  %4d", method, int(count))

        # Confidence distribution
        for conf, count in df["confidence"].value_counts().items():
            self._log.info("  confidence=%-7s  %4d", conf, int(count))

        # Per-platform breakdown
        by_platform = (
            df.groupby("platform")
              .agg(
                  rows      = ("severity",          "count"),
                  flagged   = ("is_anomaly",        "sum"),
                  high_cpa  = ("anomaly_direction", lambda x: (x == "HIGH_CPA").sum()),
                  low_cpa   = ("anomaly_direction", lambda x: (x == "LOW_CPA").sum()),
                  critical  = ("severity",          lambda x: (x == "CRITICAL").sum()),
              )
              .reset_index()
        )
        self._log.info("Per-platform breakdown:")
        for _, r in by_platform.iterrows():
            self._log.info(
                "  %-10s  rows=%3d  flagged=%2d  HIGH=%d LOW=%d  CRITICAL=%d",
                r["platform"], r["rows"], r["flagged"],
                r["high_cpa"], r["low_cpa"], r["critical"],
            )

        if n_anomaly == 0:
            self._log.info("  No anomalies detected in this window.")
            return

        top5 = (
            df[df["severity"] != "NORMAL"]
            .assign(_abs_z=lambda d: d["modified_z_score"].abs())
            .nlargest(5, "_abs_z")
        )
        self._log.info("Top anomalies (by |modified_z|):")
        for _, r in top5.iterrows():
            self._log.info(
                "  [%s] %-10s %-28s  CPA=$%7.2f  median=$%7.2f  "
                "mod_z=%+.3f  %s  (%s, %s)",
                r["date"],
                r["platform"],
                str(r["campaign_name"])[:28],
                r["observed_cpa"],
                r["rolling_mean_cpa"],
                r["modified_z_score"],
                r["severity"],
                r["baseline_method"],
                r["confidence"],
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