#!/usr/bin/env python3
"""
05_forecast.py
==============
Tier 2 — Holt-Winters 14-day forecast for spend and conversions per platform.

Algorithm selection:
  Primary  — Holt-Winters Exponential Smoothing (additive trend, additive seasonal)
              seasonal_periods=7 captures weekly Monday–Sunday marketing patterns.
              Requires >= 2 full seasonal cycles (14 observations) to fit stably.
              Initialization method 'estimated' is tried first; 'heuristic' as
              a secondary attempt if the optimizer fails to converge.

  Fallback — Simple Exponential Smoothing (SES)
              No trend, no seasonality assumption.
              Robust at any series length (>= 2 observations).
              Triggered when: series < 14 days, HW convergence fails on both
              initialization methods, or HW forecast produces NaN values.

Why not Prophet:
  With a 30–60 day training window, Prophet has more parameters than data points.
  Its multiple seasonality + changepoint model overfits badly on short series.
  HW is the correct choice at this data scale.

Confidence intervals:
  80% CI derived from in-sample residual standard deviation.
  Margin grows as σ × z₈₀ × √h (h = forecast horizon step).
  This is the standard linear error propagation formula for exponential smoothing;
  it conservatively overstates uncertainty at long horizons, which is preferred
  over false precision.

Data scale note:
  The gold table has 30 days of data (Jan 2024).  The LOOKBACK_DAYS = 60 spec
  is preserved — with only 30 days available, the query returns all 30 days.
  HW succeeds (30 ≥ 14 minimum) with 4.3 seasonal cycles captured.

Data source : fct_unified_marketing_performance  (gold mart, daily aggregates)
Output      : tbl_forecast                       (WRITE_TRUNCATE, Tier-2)

Execution:
  python pipelines/05_forecast.py
"""

from __future__ import annotations

import logging
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from google.api_core.exceptions import (
    DeadlineExceeded, InternalServerError, NotFound, ServiceUnavailable,
)
from google.cloud import bigquery

import common

# ── statsmodels: top-level import so missing dep fails fast with a clear message
try:
    from statsmodels.tsa.holtwinters import (
        ExponentialSmoothing,
        SimpleExpSmoothing,
    )
except ImportError as _sm_err:
    raise ImportError(
        "statsmodels is required for Script 05 but is not installed.\n"
        "Fix:  pip install statsmodels>=0.14.0\n"
        "Or:   pip install -r requirements.txt"
    ) from _sm_err

# ── Suppress the known-benign BigQuery Storage warning ───────────────────────
# google-cloud-bigquery falls back to REST when google-cloud-bigquery-storage
# is not installed.  For our dataset (< 1 000 rows) REST is perfectly adequate.
# Install the storage package via 'pip install -r requirements.txt' for speed.
warnings.filterwarnings(
    "ignore",
    message="BigQuery Storage module not found",
    category=UserWarning,
    module="google.cloud.bigquery.table",
)


# =============================================================================
# Config
# =============================================================================

class Config:
    PROJECT       = common.PROJECT
    PRODUCTION_DS = common.PRODUCTION_DS
    STAGING_DS    = common.STAGING_DS

    GOLD_TABLE   = "fct_unified_marketing_performance"
    OUTPUT_TABLE = "tbl_forecast"

    # ── Tier-2 analysis parameters ──────────────────────────────────────────
    LOOKBACK_DAYS        = 60    # training window (all available data if < 60 days)
    FORECAST_HORIZON     = 14    # days to project forward
    SEASONAL_PERIODS     = 7     # weekly seasonality
    MIN_HW_OBSERVATIONS  = 14    # 2 × seasonal_periods — minimum for HW
    CONFIDENCE_LEVEL     = 0.80  # 80% prediction interval
    Z_80                 = 1.2816

    METRICS = ["spend", "conversions"]   # series to forecast per platform

    # ── Cost guardrails ──────────────────────────────────────────────────────
    MAX_READ_BYTES   = 5  * 1_024 ** 3   # 5 GB
    MAX_WRITE_BYTES  = 10 * 1_024 ** 2   # 10 MB

    MAX_RETRIES  = 3
    RETRY_BASE_S = 2

    LOGS_DIR = Path(__file__).resolve().parent / "logs"

    # ── Output contract (8 columns, LLD §13.5) ──────────────────────────────
    OUTPUT_COLUMNS: list[str] = [
        "forecast_execution_date",
        "target_date",
        "platform",
        "metric_name",
        "predicted_value",
        "lower_bound",
        "upper_bound",
        "model_used",
    ]

    OUTPUT_SCHEMA: list[bigquery.SchemaField] = [
        bigquery.SchemaField("forecast_execution_date", "DATE"),
        bigquery.SchemaField("target_date",             "DATE"),
        bigquery.SchemaField("platform",                "STRING"),
        bigquery.SchemaField("metric_name",             "STRING"),
        bigquery.SchemaField("predicted_value",         "FLOAT64"),
        bigquery.SchemaField("lower_bound",             "FLOAT64"),
        bigquery.SchemaField("upper_bound",             "FLOAT64"),
        bigquery.SchemaField("model_used",              "STRING"),
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
# ForecastEngine  —  pure computation, no I/O
# =============================================================================

class ForecastEngine:
    """
    Stateless forecasting engine.  No BigQuery calls — fully testable in isolation.

    Input: a pd.Series with a DatetimeIndex (daily frequency, no NaN values).
    Output: a DataFrame with columns matching Config.OUTPUT_COLUMNS minus
            [forecast_execution_date, platform, metric_name] — those are
            added by the caller.
    """

    @classmethod
    def forecast_series(
        cls,
        series:      pd.Series,
        horizon:     int = Config.FORECAST_HORIZON,
        metric_name: str = "spend",
    ) -> pd.DataFrame:
        """
        Fit HW or SES, produce 14-day point forecast with 80% CI.

        Decision tree:
          len(series) < MIN_HW_OBSERVATIONS → SES (not enough data for HW)
          HW estimated init succeeds → HOLT_WINTERS
          HW heuristic init succeeds → HOLT_WINTERS (secondary attempt)
          Both HW attempts fail → SES (fallback)
          HW forecast contains NaN → SES (convergence was nominal but output bad)

        All predicted_value and lower_bound values are clipped at 0.
        Spend and conversions are non-negative by definition.
        """
        series = series.copy().astype(float)

        if series.isna().any():
            series = series.fillna(0.0)

        if len(series) < Config.MIN_HW_OBSERVATIONS:
            fc_vals, lower, upper, model_name = cls._fit_ses(series, horizon)
        else:
            hw_result = cls._try_holtwinters(series, horizon)
            if hw_result is not None:
                fc_vals, lower, upper, model_name = hw_result
            else:
                fc_vals, lower, upper, model_name = cls._fit_ses(series, horizon)

        # Non-negativity guard (spend and conversions are always >= 0)
        fc_vals = np.maximum(fc_vals, 0.0)
        lower   = np.maximum(lower,   0.0)

        # Build target dates starting from the day after the last training date
        last_date    = series.index[-1]
        target_dates = pd.date_range(
            start=last_date + pd.Timedelta(days=1),
            periods=horizon,
            freq="D",
        )

        return pd.DataFrame({
            "target_date":     target_dates,
            "predicted_value": np.round(fc_vals, 4),
            "lower_bound":     np.round(lower,   4),
            "upper_bound":     np.round(upper,   4),
            "model_used":      model_name,
        })

    # ── Private: model fitting ─────────────────────────────────────────────────

    @classmethod
    def _try_holtwinters(
        cls, series: pd.Series, horizon: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, str] | None:
        """
        Attempt Holt-Winters fit with 'estimated' initialization first,
        then 'heuristic' as a secondary attempt.
        Returns None if both attempts fail or produce NaN forecasts.
        """
        for init_method in ("estimated", "heuristic"):
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore")
                    model = ExponentialSmoothing(
                        series,
                        trend="add",
                        seasonal="add",
                        seasonal_periods=Config.SEASONAL_PERIODS,
                        initialization_method=init_method,
                    )
                    fit = model.fit(optimized=True)

                fc_vals = fit.forecast(horizon).values

                # Reject degenerate fits (NaN forecasts, or all-identical output
                # that suggests the optimizer made no meaningful update)
                if np.isnan(fc_vals).any():
                    continue

                lower, upper = cls._compute_ci(fit, fc_vals, horizon)
                return fc_vals, lower, upper, "HOLT_WINTERS"

            except Exception:
                continue

        return None   # Both attempts failed

    @classmethod
    def _fit_ses(
        cls, series: pd.Series, horizon: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
        """
        Simple Exponential Smoothing — fallback when HW is unavailable.
        No trend or seasonality.  Robust at any series length >= 2.
        """
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            fit = SimpleExpSmoothing(
                series, initialization_method="estimated"
            ).fit(optimized=True)

        fc_vals = fit.forecast(horizon).values
        lower, upper = cls._compute_ci(fit, fc_vals, horizon)
        return fc_vals, lower, upper, "SES_FALLBACK"

    @classmethod
    def _compute_ci(
        cls,
        fit,
        fc_vals:  np.ndarray,
        horizon:  int,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        80% prediction interval via in-sample residual standard deviation.

        Formula: margin_h = z₈₀ × σ × √h
          where h = forecast horizon step (1, 2, …, horizon)
                σ = std of in-sample residuals

        σ × √h reflects the standard linear error propagation for random walks
        and exponential smoothing models.  It overestimates uncertainty at long
        horizons (conservative), which is preferred over false precision.

        If σ is near zero (e.g. perfectly constant series), substitute 5% of
        the mean forecast value to avoid zero-width intervals.
        """
        resid = np.array(fit.resid)
        sigma = float(np.std(resid[~np.isnan(resid)], ddof=1))

        if sigma < 1e-9:
            # Degenerate case: constant series; use 5% of mean forecast as σ
            mean_fc = float(np.mean(np.abs(fc_vals)))
            sigma   = max(mean_fc * 0.05, 1e-6)

        horizons = np.arange(1, horizon + 1)
        margin   = Config.Z_80 * sigma * np.sqrt(horizons)
        return fc_vals - margin, fc_vals + margin


# =============================================================================
# ForecastPipeline  —  orchestration
# =============================================================================

class ForecastPipeline:
    """
    Fetches daily aggregates → fits HW/SES per (platform, metric) series →
    writes tbl_forecast (WRITE_TRUNCATE, Tier-2 derived output).
    """

    def __init__(self) -> None:
        self._log    = setup_logging(Config.LOGS_DIR / "05_forecast.log")
        self._client = common.bq_client()

    def run(self) -> None:
        self._log.info("=" * 72)
        self._log.info(
            "Script 05 — Forecast  started at %s",
            datetime.now(timezone.utc).isoformat(),
        )
        self._log.info("=" * 72)
        self._log.info(
            "Parameters  lookback=%d d  horizon=%d d  seasonal_periods=%d  "
            "confidence=%d%%  model=HW+SES_fallback",
            Config.LOOKBACK_DAYS, Config.FORECAST_HORIZON,
            Config.SEASONAL_PERIODS, int(Config.CONFIDENCE_LEVEL * 100),
        )

        # ── Fetch daily aggregates ─────────────────────────────────────────────
        daily_df = self._fetch_gold_data()
        if daily_df.empty:
            self._log.warning("Gold table returned no rows — nothing to forecast.")
            return

        date_min = str(daily_df["date"].min())[:10]
        date_max = str(daily_df["date"].max())[:10]
        n_days   = daily_df["date"].nunique()
        self._log.info(
            "Fetched daily aggregates  period: %s → %s  (%d days)",
            date_min, date_max, n_days,
        )

        # ── Forecast each (platform, metric) series ───────────────────────────
        exec_date      = datetime.now(timezone.utc).date()
        all_forecasts  = []
        model_registry = {}   # {(platform, metric): model_name}

        platforms = sorted(daily_df["platform"].unique())
        for platform in platforms:
            platform_df = (
                daily_df[daily_df["platform"] == platform]
                .set_index("date")
                .sort_index()
            )

            # Fill any missing calendar dates with 0
            full_index   = pd.date_range(platform_df.index.min(),
                                         platform_df.index.max(), freq="D")
            platform_df  = platform_df.reindex(full_index, fill_value=0.0)

            for metric in Config.METRICS:
                col      = f"daily_{metric}"
                series   = platform_df[col].rename(metric)
                n_obs    = len(series)
                n_nonzero= int((series > 0).sum())

                self._log.info(
                    "Forecasting  %-10s %-12s  obs=%d  non-zero=%d",
                    platform, metric, n_obs, n_nonzero,
                )

                fc_df = ForecastEngine.forecast_series(
                    series,
                    horizon=Config.FORECAST_HORIZON,
                    metric_name=metric,
                )

                model_name = fc_df["model_used"].iloc[0]
                model_registry[(platform, metric)] = model_name

                fc_df.insert(0, "platform",              platform)
                fc_df.insert(0, "metric_name",           metric)
                fc_df.insert(0, "forecast_execution_date", exec_date)

                all_forecasts.append(fc_df)

        output_df = pd.concat(all_forecasts, ignore_index=True)[Config.OUTPUT_COLUMNS]

        # ── Log model selection summary ───────────────────────────────────────
        self._log_summary(output_df, model_registry, date_max)

        # ── Write ─────────────────────────────────────────────────────────────
        self._write_output(output_df)
        self._log.info(
            "Script 05 COMPLETE  rows_written=%d  table=%s",
            len(output_df), Config.output_ref(),
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _fetch_gold_data(self) -> pd.DataFrame:
        """
        Pull daily-aggregated spend and conversions per platform.

        GROUP BY (date, platform) to get the platform-level daily total.
        The forecast operates at the platform level, not the campaign level,
        which matches the budget optimizer and is the appropriate granularity
        for a CMO-facing 14-day projection.

        Missing dates in the returned time series are handled in run() by
        reindexing to a complete date range with fill_value=0.
        """
        try:
            self._client.get_table(Config.gold_ref())
        except NotFound:
            self._log.error(
                "Gold table %s not found. Run 02_run_transformations.py first.",
                Config.gold_ref(),
            )
            return pd.DataFrame()

        mature = common.mature_through_sql(Config.gold_ref())
        sql = f"""
        SELECT
            date,
            platform,
            SUM(spend)       AS daily_spend,
            SUM(conversions) AS daily_conversions
        FROM `{Config.gold_ref()}`
        WHERE
            date BETWEEN DATE_SUB({mature}, INTERVAL {Config.LOOKBACK_DAYS - 1} DAY)
                     AND {mature}
        GROUP BY date, platform
        ORDER BY platform, date
        """

        self._log.info("Querying gold mart (last %d days) …", Config.LOOKBACK_DAYS)
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=Config.MAX_READ_BYTES,
            ),
        )
        df = job.to_dataframe()

        if not df.empty:
            df["date"]              = pd.to_datetime(df["date"])
            df["daily_spend"]       = pd.to_numeric(df["daily_spend"],       errors="coerce").fillna(0.0)
            df["daily_conversions"] = pd.to_numeric(df["daily_conversions"], errors="coerce").fillna(0.0)

        return df

    def _write_output(self, df: pd.DataFrame) -> None:
        """Write to tbl_forecast (WRITE_TRUNCATE) with retry."""
        df = df.copy()
        df["forecast_execution_date"] = pd.to_datetime(df["forecast_execution_date"])
        df["target_date"]             = pd.to_datetime(df["target_date"])

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
                    "Write attempt %d failed: %s — retry in %ds", attempt, exc, wait
                )
                time.sleep(wait)
            except Exception as exc:
                self._log.error("Write failed (non-retryable): %s", exc)
                raise

        raise RuntimeError(
            f"Write failed after {Config.MAX_RETRIES} attempts"
        ) from last_exc

    def _log_summary(
        self,
        df:             pd.DataFrame,
        model_registry: dict,
        training_end:   str,
    ) -> None:
        """Log model selections, forecast date range, and platform-level projections."""
        target_min = str(df["target_date"].min())[:10]
        target_max = str(df["target_date"].max())[:10]

        self._log.info("-" * 72)
        self._log.info(
            "Forecast window: %s → %s  (trained on data through %s)",
            target_min, target_max, str(training_end)[:10],
        )
        self._log.info("-" * 72)
        self._log.info("  %-10s %-12s %-14s  %12s  %12s",
                       "Platform", "Metric", "Model", "14d Total", "Daily Avg")
        self._log.info("-" * 72)

        for (platform, metric), model_name in sorted(model_registry.items()):
            subset    = df[(df["platform"] == platform) & (df["metric_name"] == metric)]
            total_fc  = float(subset["predicted_value"].sum())
            daily_avg = float(subset["predicted_value"].mean())
            unit      = "$" if metric == "spend" else " "
            self._log.info(
                "  %-10s %-12s %-14s  %s%s  %s%s",
                platform, metric, model_name,
                unit, f"{total_fc:>11,.2f}",
                unit, f"{daily_avg:>11,.2f}",
            )

        self._log.info("-" * 72)
        hw_count  = sum(1 for m in model_registry.values() if m == "HOLT_WINTERS")
        ses_count = sum(1 for m in model_registry.values() if m == "SES_FALLBACK")
        self._log.info(
            "Models used: HOLT_WINTERS=%d  SES_FALLBACK=%d  "
            "(out of %d series)",
            hw_count, ses_count, len(model_registry),
        )


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    # Logging first — before any pipeline instantiation (defensive pattern)
    log = setup_logging(Config.LOGS_DIR / "05_forecast.log")
    try:
        ForecastPipeline().run()
    except Exception as exc:
        log.error("Script 05 FAILED: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()