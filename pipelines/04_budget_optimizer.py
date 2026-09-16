#!/usr/bin/env python3
"""
04_budget_optimizer.py
======================
Tier 2 — Linear programming budget allocation optimizer.

Objective: Given a fixed total budget, maximise projected conversions by
           reallocating spend across platforms according to each platform's
           30-day aggregate CPA (Cost Per Acquisition).

Algorithm: scipy.optimize.linprog (HiGHS solver — continuous LP).

           Maximise:  ∑ (budget_i / CPA_i)      [total projected conversions]
           Subject to:
             ∑ budget_i       = total_budget      [fixed spend envelope]
             budget_i        ≥ 0.10 × total       [10% multi-channel floor]
             budget_i        ≤ 2.0 × current_i    [cap at 2× current spend]

           linprog minimises, so objective is negated:
             Minimise: −∑ (budget_i × efficiency_i)
             where efficiency_i = 1 / CPA_i (conversions per dollar)

Critical implementation note (LLD §12.4):
  Platform-to-coefficient alignment is EXPLICIT. After GROUP BY in BigQuery,
  row order is non-deterministic. The DataFrame is sorted by platform name
  BEFORE building the LP coefficient and bounds arrays, and the result vector
  is mapped back to platforms by the same sorted index.
  A silent mislabelling here would recommend the TikTok budget go to Facebook
  — the exact opposite of the correct recommendation.

Assumptions (stated in every output row):
  CPA is assumed constant at the 30-day aggregate average.
  In reality, doubling Facebook spend may encounter audience saturation and
  increase CPA. This model is a valid first-order allocation signal; it does
  not capture diminishing returns or bid competition effects.

Data source : fct_unified_marketing_performance  (gold mart, 30-day window)
Output      : tbl_budget_recommendations         (WRITE_TRUNCATE, Tier-2)

Execution:
  python pipelines/04_budget_optimizer.py
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from google.api_core.exceptions import (
    DeadlineExceeded, InternalServerError, NotFound, ServiceUnavailable,
)
from google.cloud import bigquery

import common


# =============================================================================
# Config
# =============================================================================

class Config:
    PROJECT       = common.PROJECT
    PRODUCTION_DS = common.PRODUCTION_DS
    STAGING_DS    = common.STAGING_DS

    GOLD_TABLE   = "fct_unified_marketing_performance"
    OUTPUT_TABLE = "tbl_budget_recommendations"

    # ── Tier-2 analysis parameters ──────────────────────────────────────────
    LOOKBACK_DAYS    = 30     # aggregate CPA window
    FLOOR_PCT        = 0.10   # minimum budget per platform as % of total
    CAP_MULTIPLIER   = 2.0    # maximum budget per platform as × current spend

    # ── Cost guardrails ──────────────────────────────────────────────────────
    MAX_READ_BYTES   = 5   * 1_024 ** 3   # 5 GB
    MAX_WRITE_BYTES  = 10  * 1_024 ** 2   # 10 MB (tiny derived table)

    MAX_RETRIES  = 3
    RETRY_BASE_S = 2

    LOGS_DIR = Path(__file__).resolve().parent / "logs"

    # ── Output contract (13 columns, matching LLD §12.3) ────────────────────
    OUTPUT_COLUMNS: list[str] = [
        "generated_at",
        "analysis_period_start",
        "analysis_period_end",
        "platform",
        "current_spend",
        "current_spend_pct",
        "current_conversions",
        "current_cpa",
        "recommended_spend",
        "recommended_spend_pct",
        "projected_conversions",
        "conversion_delta",
        "assumption_note",
    ]

    OUTPUT_SCHEMA: list[bigquery.SchemaField] = [
        bigquery.SchemaField("generated_at",          "TIMESTAMP"),
        bigquery.SchemaField("analysis_period_start", "DATE"),
        bigquery.SchemaField("analysis_period_end",   "DATE"),
        bigquery.SchemaField("platform",              "STRING"),
        bigquery.SchemaField("current_spend",         "FLOAT64"),
        bigquery.SchemaField("current_spend_pct",     "FLOAT64"),
        bigquery.SchemaField("current_conversions",   "FLOAT64"),
        bigquery.SchemaField("current_cpa",           "FLOAT64"),
        bigquery.SchemaField("recommended_spend",     "FLOAT64"),
        bigquery.SchemaField("recommended_spend_pct", "FLOAT64"),
        bigquery.SchemaField("projected_conversions", "FLOAT64"),
        bigquery.SchemaField("conversion_delta",      "FLOAT64"),
        bigquery.SchemaField("assumption_note",       "STRING"),
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
# BudgetOptimizerEngine  —  pure computation, no I/O
# =============================================================================

class BudgetOptimizerEngine:
    """
    Stateless LP optimizer.  No BigQuery calls — fully testable in isolation.

    Input DataFrame columns:
      platform, total_spend, total_conversions, period_start, period_end

    Output: same DataFrame extended with LP recommendation columns.
    """

    @classmethod
    def optimize(
        cls,
        df:          pd.DataFrame,
        floor_pct:   float = Config.FLOOR_PCT,
        cap_mult:    float = Config.CAP_MULTIPLIER,
    ) -> pd.DataFrame:
        """
        Run the LP and return a complete recommendation DataFrame.

        Critical alignment guarantee:
          df MUST be sorted by platform name before this method is called.
          The LP coefficient array c and bounds array are built in df's current
          row order.  result.x[i] maps to df.iloc[i] by position.
          Any other ordering silently misassigns budgets.

        Args:
            df:         Platform aggregate data. Must be sorted by platform.
            floor_pct:  Minimum budget fraction per platform (default 10%).
            cap_mult:   Maximum budget multiplier vs current (default 2×).

        Returns:
            df extended with [recommended_spend, recommended_spend_pct,
                              projected_conversions, conversion_delta].

        Raises:
            ValueError: if fewer than 1 platform, or constraints are infeasible.
            RuntimeError: if the LP solver does not find an optimal solution.
        """
        cls._validate_input(df)

        n            = len(df)
        total_budget = float(df["total_spend"].sum())
        spend        = df["total_spend"].values.astype(float)
        conversions  = df["total_conversions"].values.astype(float)
        cpa          = spend / conversions                       # aggregate CPA per platform
        efficiency   = 1.0 / cpa                                 # conversions per dollar

        # Constraints
        min_spend_each = floor_pct * total_budget
        max_spend_each = cap_mult  * spend                       # per-platform cap

        # Pre-flight feasibility check (fast, before calling linprog)
        cls._check_feasibility(n, total_budget, min_spend_each, max_spend_each, spend)

        # ── LP formulation ────────────────────────────────────────────────────
        # Minimise: -efficiency @ x  (equivalent to maximise conversions)
        c      = -efficiency                                      # shape (n,)
        A_eq   = np.ones((1, n))                                  # sum(x) = total
        b_eq   = np.array([total_budget])
        bounds = [(min_spend_each, float(cap_i)) for cap_i in max_spend_each]

        result = linprog(
            c,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
            options={"disp": False},
        )

        # HiGHS status 0 = optimal.  Any other status is a problem.
        if result.status != 0:
            raise RuntimeError(
                f"LP solver returned non-optimal status {result.status}: "
                f"{result.message}"
            )

        rec_spend = result.x.copy()

        # ── Normalisation (floating-point guard) ─────────────────────────────
        # linprog with equality constraint should give exact sum = total_budget.
        # If floating-point drift makes sum differ by > $0.01, something is wrong.
        spend_diff = abs(rec_spend.sum() - total_budget)
        if spend_diff > 0.01:
            raise RuntimeError(
                f"LP solution total ${rec_spend.sum():,.2f} differs from "
                f"budget ${total_budget:,.2f} by ${spend_diff:.4f} — unexpected."
            )
        # Tiny drift (< $0.01): redistribute proportionally to enforce exact sum
        if spend_diff > 1e-9:
            rec_spend = rec_spend * (total_budget / rec_spend.sum())

        # ── Build recommendation columns ──────────────────────────────────────
        # All in-place mutations are on a copy; original df is unchanged.
        out = df.copy()
        out["current_spend_pct"]     = (spend / total_budget * 100).round(2)
        out["current_cpa"]           = cpa.round(4)
        out["recommended_spend"]     = rec_spend.round(2)
        out["recommended_spend_pct"] = (rec_spend / total_budget * 100).round(2)
        out["projected_conversions"] = (rec_spend / cpa).round(2)
        out["conversion_delta"]      = (out["projected_conversions"] - conversions).round(2)

        return out

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _validate_input(df: pd.DataFrame) -> None:
        required = {"platform", "total_spend", "total_conversions"}
        missing  = required - set(df.columns)
        if missing:
            raise ValueError(f"Input DataFrame missing columns: {missing}")
        if len(df) == 0:
            raise ValueError("No platforms with valid data — cannot optimise.")
        if (df["total_conversions"] <= 0).any():
            raise ValueError(
                "All platforms must have positive total_conversions. "
                f"Platforms with zero or negative: "
                f"{df[df['total_conversions'] <= 0]['platform'].tolist()}"
            )
        # Zero-spend guard: CPA = 0 / spend = 0 → efficiency = 1/0 = ZeroDivisionError
        if (df["total_spend"] <= 0).any():
            raise ValueError(
                "All platforms must have positive total_spend. "
                f"Platforms with zero or negative: "
                f"{df[df['total_spend'] <= 0]['platform'].tolist()}"
            )
        # Verify alphabetical sort (alignment critical)
        names = list(df["platform"])
        if names != sorted(names):
            raise ValueError(
                f"Input DataFrame must be sorted by platform name. "
                f"Got {names}, expected {sorted(names)}."
            )

    @staticmethod
    def _check_feasibility(
        n: int,
        total_budget: float,
        min_spend_each: float,
        max_spend_each: np.ndarray,
        current_spend: np.ndarray,
    ) -> None:
        """Raise early with a clear message if LP constraints cannot be satisfied."""
        sum_floors = min_spend_each * n
        sum_caps   = float(max_spend_each.sum())

        if sum_floors > total_budget + 0.01:
            raise ValueError(
                f"LP is infeasible: sum of floor budgets (${sum_floors:,.2f}) "
                f"exceeds total budget (${total_budget:,.2f}). "
                f"Reduce FLOOR_PCT or increase platform count."
            )
        if sum_caps < total_budget - 0.01:
            raise ValueError(
                f"LP is infeasible: sum of cap budgets (${sum_caps:,.2f}) "
                f"is less than total budget (${total_budget:,.2f}). "
                f"Increase CAP_MULTIPLIER."
            )
        for i in range(n):
            if min_spend_each > max_spend_each[i] + 0.01:
                raise ValueError(
                    f"Platform at index {i} has floor (${min_spend_each:,.2f}) "
                    f"exceeding its cap (${max_spend_each[i]:,.2f}). "
                    f"LP is infeasible for this platform."
                )


# =============================================================================
# BudgetOptimizerPipeline  —  orchestration
# =============================================================================

class BudgetOptimizerPipeline:
    """
    Reads 30-day gold aggregates → runs LP → writes tbl_budget_recommendations.
    WRITE_TRUNCATE: derived Tier-2 output, recomputed fresh each run.
    """

    def __init__(self) -> None:
        self._log    = setup_logging(Config.LOGS_DIR / "04_budget_optimizer.log")
        self._client = common.bq_client()

    def run(self) -> None:
        self._log.info("=" * 72)
        self._log.info(
            "Script 04 — Budget Optimizer  started at %s",
            datetime.now(timezone.utc).isoformat(),
        )
        self._log.info("=" * 72)
        self._log.info(
            "Parameters  lookback=%d d  floor=%.0f%%  cap=%.1f× current",
            Config.LOOKBACK_DAYS, Config.FLOOR_PCT * 100, Config.CAP_MULTIPLIER,
        )

        # ── Fetch 30-day aggregates ────────────────────────────────────────────
        raw_df = self._fetch_gold_data()
        if raw_df.empty:
            self._log.warning("Gold table returned no rows — cannot optimise.")
            return

        self._log.info(
            "Fetched %d-platform aggregates  |  period: %s → %s  |  "
            "total spend: $%.2f  total conversions: %s",
            len(raw_df),
            raw_df["period_start"].min(), raw_df["period_end"].max(),
            raw_df["total_spend"].sum(),
            f"{int(raw_df['total_conversions'].sum()):,}",
        )

        # ── Sort by platform name BEFORE building LP arrays ──────────────────
        # This is the critical alignment step. GROUP BY in BigQuery does not
        # guarantee row order. Building LP coefficients from unsorted data
        # would silently assign the wrong budget to each platform.
        raw_df = raw_df.sort_values("platform").reset_index(drop=True)
        self._log.info(
            "Platform order (LP alignment): %s",
            list(raw_df["platform"]),
        )

        # ── Run LP optimiser ──────────────────────────────────────────────────
        self._log.info("Running LP optimisation (scipy.optimize.linprog / HiGHS)…")
        result_df = BudgetOptimizerEngine.optimize(raw_df)
        self._log.info("LP solved — status: optimal")

        # ── Build output DataFrame ────────────────────────────────────────────
        output_df = self._build_output(result_df)

        # ── Log recommendation table ──────────────────────────────────────────
        self._log_summary(output_df)

        # ── Write to BigQuery ─────────────────────────────────────────────────
        self._write_output(output_df)
        self._log.info(
            "Script 04 COMPLETE  rows_written=%d  table=%s",
            len(output_df), Config.output_ref(),
        )

    # ── Private ──────────────────────────────────────────────────────────────

    def _fetch_gold_data(self) -> pd.DataFrame:
        """
        Pull 30-day aggregate spend and conversions per platform.

        Aggregate CPA = SUM(spend) / SUM(conversions) at the platform level.
        This is the correct basis for the LP: the marginal efficiency of the
        NEXT dollar to this platform, approximated by historical average.

        Do NOT use AVG(cpa) — that would average daily CPAs which weights
        high-volume days the same as low-volume days (mathematically wrong).

        HAVING SUM(conversions) > 0 AND SUM(spend) > 0:
          excludes platforms with no conversions (CPA undefined) AND
          platforms with zero spend (CPA = 0 → efficiency = 1/0 = ZeroDivisionError).

        ORDER BY platform: explicit deterministic sort applied here AND in
        Python after fetch (belt-and-suspenders for alignment safety).
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
            platform,
            SUM(spend)              AS total_spend,
            SUM(conversions)        AS total_conversions,
            MIN(date)               AS period_start,
            MAX(date)               AS period_end
        FROM `{Config.gold_ref()}`
        WHERE
            date >= DATE_SUB(
                (SELECT MAX(date) FROM `{Config.gold_ref()}`),
                INTERVAL {Config.LOOKBACK_DAYS - 1} DAY
            )
        GROUP BY platform
        HAVING SUM(conversions) > 0
           AND SUM(spend) > 0
        ORDER BY platform
        """

        self._log.info("Querying gold mart (last %d days) …", Config.LOOKBACK_DAYS)
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=Config.MAX_READ_BYTES,
            ),
        )
        df = job.to_dataframe()

        if df.empty:
            return df

        # Ensure numeric types (to_dataframe from BQ should handle this, but be defensive)
        df["total_spend"]       = pd.to_numeric(df["total_spend"],       errors="coerce")
        df["total_conversions"] = pd.to_numeric(df["total_conversions"], errors="coerce")
        df = df.dropna(subset=["total_spend", "total_conversions"])

        return df

    def _build_output(self, result_df: pd.DataFrame) -> pd.DataFrame:
        """
        Build the output DataFrame with all 13 columns — platform rows ONLY.

        Gap 1 fix: the TOTAL summary row has been deliberately removed from
        the BigQuery write.  Adding platform='TOTAL' breaks any SUM() query
        downstream: SELECT SUM(recommended_spend) would double-count since the
        TOTAL row already includes the sum of all platform rows.
        Totals are displayed in the terminal log (_log_summary) but are computed
        dynamically from the platform rows — never written to BigQuery.
        """
        now     = datetime.now(timezone.utc)
        p_start = str(result_df["period_start"].min())
        p_end   = str(result_df["period_end"].max())

        assumption = (
            f"CPA assumed constant at {Config.LOOKBACK_DAYS}-day aggregate average. "
            f"Constraints: min {Config.FLOOR_PCT*100:.0f}% of total per platform, "
            f"max {Config.CAP_MULTIPLIER:.1f}× current spend per platform. "
            "Does not model diminishing returns or bid competition effects."
        )

        rows = []
        for _, r in result_df.iterrows():
            rows.append({
                "generated_at":          now,
                "analysis_period_start": p_start,
                "analysis_period_end":   p_end,
                "platform":              r["platform"],
                "current_spend":         round(float(r["total_spend"]),         2),
                "current_spend_pct":     round(float(r["current_spend_pct"]),   2),
                "current_conversions":   round(float(r["total_conversions"]),   2),
                "current_cpa":           round(float(r["current_cpa"]),         4),
                "recommended_spend":     round(float(r["recommended_spend"]),   2),
                "recommended_spend_pct": round(float(r["recommended_spend_pct"]), 2),
                "projected_conversions": round(float(r["projected_conversions"]), 2),
                "conversion_delta":      round(float(r["conversion_delta"]),    2),
                "assumption_note":       assumption,
            })

        # TOTAL row intentionally excluded from BQ write (see docstring above).
        # Terminal summary is computed in _log_summary from these platform rows.
        return pd.DataFrame(rows)[Config.OUTPUT_COLUMNS]

    def _write_output(self, df: pd.DataFrame) -> None:
        """Write to tbl_budget_recommendations (WRITE_TRUNCATE) with retry."""
        # Convert timestamp column for pyarrow DATE compatibility
        df = df.copy()
        df["generated_at"] = pd.to_datetime(df["generated_at"], utc=True)
        df["analysis_period_start"] = pd.to_datetime(df["analysis_period_start"])
        df["analysis_period_end"]   = pd.to_datetime(df["analysis_period_end"])

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

    def _log_summary(self, df: pd.DataFrame) -> None:
        """Log the complete recommendation table and uplift summary.

        df contains only platform rows — TOTAL row is not written to BigQuery
        (Gap 1 fix).  Totals are computed dynamically from the platform rows.

        Note: logging.info() uses Python's %-operator for interpolation which
        does NOT support the comma thousands-separator (%,.2f raises ValueError).
        All numeric values are pre-formatted with f-strings before passing to log.
        """
        # Compute totals from the 3 platform rows (no TOTAL row in df)
        total_curr_spend = float(df["current_spend"].sum())
        total_rec_spend  = float(df["recommended_spend"].sum())
        total_curr_conv  = float(df["current_conversions"].sum())
        total_delta      = float(df["conversion_delta"].sum())
        uplift_pct       = total_delta / total_curr_conv * 100 if total_curr_conv else 0

        self._log.info("-" * 72)
        self._log.info("  %-10s  %12s  %5s    %12s  %5s    %10s",
                       "Platform", "Current $", "Shr%", "Recom. $", "Shr%", "Conv Δ")
        self._log.info("-" * 72)

        for _, r in df.iterrows():
            line = (
                f"  {r['platform']:<10}"
                f"  ${float(r['current_spend']):>12,.2f}  {float(r['current_spend_pct']):>5.1f}%"
                f"    ${float(r['recommended_spend']):>12,.2f}  {float(r['recommended_spend_pct']):>5.1f}%"
                f"    {float(r['conversion_delta']):>+10,.0f}"
            )
            self._log.info(line)

        self._log.info("-" * 72)
        total_line = (
            f"  {'TOTAL':<10}"
            f"  ${total_curr_spend:>12,.2f}  {'100.0':>5}%"
            f"    ${total_rec_spend:>12,.2f}  {'100.0':>5}%"
            f"    {total_delta:>+10,.0f}  ({uplift_pct:+.1f}%)"
        )
        self._log.info(total_line)
        self._log.info("-" * 72)
        # Fix: use single % not %% (in f-strings, %% = two literal percent chars)
        self._log.info(
            "Uplift: %s conversions  (%s)  at same total spend of $%s",
            f"{total_delta:+,.0f}",
            f"{uplift_pct:+.1f}%",
            f"{total_curr_spend:,.2f}",
        )
        self._log.info("Note: %s", df.iloc[0]["assumption_note"])


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    # Gap 3 fix: setup_logging is the absolute first step.
    # If BudgetOptimizerPipeline() itself raises (e.g. BQ auth failure)
    # the handler is already attached and the error is captured correctly.
    log = setup_logging(Config.LOGS_DIR / "04_budget_optimizer.log")
    try:
        BudgetOptimizerPipeline().run()
    except Exception as exc:
        log.error("Script 04 FAILED: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()