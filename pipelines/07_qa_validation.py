"""
Script 07 — QA Validation Runner
=================================
Read-only reconciliation script that verifies the bronze → gold pipeline
produced consistent, complete, correct output. Touches zero rows — only reads.

Runs AFTER scripts 01–05 have finished. Produces a structured log of
pass/fail results and exits with code 0 (all critical checks passed) or
1 (at least one critical check failed).

Usage:
    python pipelines/07_qa_validation.py

Exit codes:
    0 — All critical checks passed
    1 — At least one critical check failed
"""

import logging
import sys
from google.cloud import bigquery

import common


# ---------------------------------------------------------------------------
# Logging — matches 03/04/05 format exactly
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    handlers=common.log_handlers("07_qa_validation"),
)


# ---------------------------------------------------------------------------
# Config — matches other pipeline scripts
# ---------------------------------------------------------------------------
class Config:
    PROJECT            = common.PROJECT
    BRONZE_DATASET     = f"{PROJECT}.{common.BRONZE_DS}"
    STAGING_DATASET    = f"{PROJECT}.{common.STAGING_DS}"
    PRODUCTION_DATASET = f"{PROJECT}.{common.PRODUCTION_DS}"

    GOLD_TABLE         = f"{PRODUCTION_DATASET}.fct_unified_marketing_performance"
    QUARANTINE_TABLE   = f"{STAGING_DATASET}.stg_quarantine_logs"
    AUDIT_TABLE        = f"{STAGING_DATASET}.tbl_ingestion_audit"

    BRONZE_TABLES = {
        "Facebook": f"{BRONZE_DATASET}.facebook_ads_landing",
        "Google":   f"{BRONZE_DATASET}.google_ads_landing",
        "TikTok":   f"{BRONZE_DATASET}.tiktok_ads_landing",
    }

    # Bronze spend column varies by platform (LLD Section 9 column mapping):
    # Facebook uses "spend", Google and TikTok use "cost".
    BRONZE_SPEND_COLUMNS = {
        "Facebook": "spend",
        "Google":   "cost",
        "TikTok":   "cost",
    }

    ML_TABLES = [
        f"{STAGING_DATASET}.fct_anomaly_flags",
        f"{STAGING_DATASET}.tbl_budget_recommendations",
        f"{STAGING_DATASET}.tbl_forecast",
    ]

    EXPECTED_GOLD_COLUMNS = 30     # Data Contract — hardcoded
    SPEND_TOLERANCE       = 0.01   # $ tolerance for reconciliation

    MAX_READ_BYTES = 5 * 1_024 ** 3  # 5 GB — Tier 2 read cap


# ---------------------------------------------------------------------------
# QA Validation Runner
# ---------------------------------------------------------------------------
class QAValidationRunner:
    """Executes 9 read-only QA checks against BigQuery and reports results."""

    # Severity constants
    CRITICAL = "CRITICAL"
    WARNING  = "WARNING"
    INFO     = "INFO"

    def __init__(self):
        self._log = logging.getLogger(self.__class__.__name__)
        self._client = common.bq_client()
        self._critical_failures: list[str] = []
        self._warnings: list[str] = []
        self._info_messages: list[str] = []
        self._results: list[dict] = []  # For summary table

    # ------------------------------------------------------------------
    # Helper — query with cost guardrail
    # ------------------------------------------------------------------
    def _run_query(self, sql: str) -> bigquery.table.RowIterator:
        """Execute a read-only query with a byte-billing cap."""
        job_config = bigquery.QueryJobConfig(
            maximum_bytes_billed=Config.MAX_READ_BYTES,
            use_legacy_sql=False,
        )
        return self._client.query(sql, job_config=job_config).result()

    def _record(self, check_num: int, name: str, status: str, detail: str):
        """Record a check result for the summary table."""
        self._results.append({
            "check": check_num,
            "name": name,
            "status": status,
            "detail": detail,
        })

    # ------------------------------------------------------------------
    # Check 1: Row count reconciliation
    # ------------------------------------------------------------------
    def _check_row_count_reconciliation(self):
        """Check 1: gold rows + gold quarantine rows == sum(bronze rows)."""
        self._log.info("[1/9] Row count reconciliation")

        # Total bronze rows (sum across all 3 platforms)
        bronze_sql = """
        SELECT
            (SELECT COUNT(*) FROM `{fb}`) +
            (SELECT COUNT(*) FROM `{gg}`) +
            (SELECT COUNT(*) FROM `{tt}`) AS total_bronze_rows
        """.format(
            fb=Config.BRONZE_TABLES["Facebook"],
            gg=Config.BRONZE_TABLES["Google"],
            tt=Config.BRONZE_TABLES["TikTok"],
        )
        bronze_total = list(self._run_query(bronze_sql))[0].total_bronze_rows

        # Actual gold rows
        gold_sql = f"""
        SELECT COUNT(*) AS actual_gold_rows
        FROM `{Config.GOLD_TABLE}`
        """
        gold_rows = list(self._run_query(gold_sql))[0].actual_gold_rows

        # Gold quarantine rows (rows excluded during gold validation)
        quarantine_sql = f"""
        SELECT COUNT(*) AS gold_quarantine_rows
        FROM `{Config.QUARANTINE_TABLE}`
        WHERE origin_platform IN (
            'facebook_gold_validation',
            'google_gold_validation',
            'tiktok_gold_validation'
        )
        """
        gold_quarantine = list(self._run_query(quarantine_sql))[0].gold_quarantine_rows

        reconciled = gold_rows + gold_quarantine
        self._log.info(
            "  Bronze total: %d  |  Gold: %d  |  Gold quarantine: %d",
            bronze_total, gold_rows, gold_quarantine,
        )

        if reconciled == bronze_total:
            msg = f"gold + quarantine == bronze ({reconciled} == {bronze_total})"
            self._log.info("  PASS — %s", msg)
            self._record(1, "Row count reconciliation", "PASS", msg)
        else:
            msg = (
                f"gold + quarantine ({reconciled}) != bronze ({bronze_total}) "
                f"— {abs(bronze_total - reconciled)} rows unaccounted"
            )
            self._log.error("  FAIL — %s", msg)
            self._critical_failures.append(msg)
            self._record(1, "Row count reconciliation", "FAIL", msg)

    # ------------------------------------------------------------------
    # Check 2: Column count (Data Contract)
    # ------------------------------------------------------------------
    def _check_column_count(self):
        """Check 2: gold table has exactly 30 columns (Data Contract)."""
        self._log.info("[2/9] Column count (Data Contract)")

        sql = f"""
        SELECT COUNT(*) AS col_count
        FROM `{Config.PRODUCTION_DATASET}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = 'fct_unified_marketing_performance'
        """
        col_count = list(self._run_query(sql))[0].col_count

        if col_count == Config.EXPECTED_GOLD_COLUMNS:
            msg = f"gold has {col_count} columns (expected {Config.EXPECTED_GOLD_COLUMNS})"
            self._log.info("  PASS — %s", msg)
            self._record(2, "Column count (Data Contract)", "PASS", msg)
        else:
            msg = (
                f"gold has {col_count} columns "
                f"(expected {Config.EXPECTED_GOLD_COLUMNS})"
            )
            self._log.error("  FAIL — %s", msg)
            self._critical_failures.append(msg)
            self._record(2, "Column count (Data Contract)", "FAIL", msg)

    # ------------------------------------------------------------------
    # Check 3: Per-platform spend reconciliation
    # ------------------------------------------------------------------
    def _check_spend_reconciliation(self):
        """Check 3: per-platform spend matches within $0.01."""
        self._log.info("[3/9] Per-platform spend reconciliation")

        all_passed = True
        for platform, bronze_table in Config.BRONZE_TABLES.items():
            # Bronze spend column varies: Facebook="spend", Google/TikTok="cost"
            bronze_spend_col = Config.BRONZE_SPEND_COLUMNS[platform]

            # Bronze spend (SAFE_CAST — all-STRING landing tables)
            bronze_sql = f"""
            SELECT COALESCE(SUM(SAFE_CAST({bronze_spend_col} AS FLOAT64)), 0) AS total_spend
            FROM `{bronze_table}`
            """
            bronze_spend = list(self._run_query(bronze_sql))[0].total_spend

            # Gold spend
            gold_sql = f"""
            SELECT COALESCE(SUM(spend), 0) AS total_spend
            FROM `{Config.GOLD_TABLE}`
            WHERE platform = '{platform}'
            """
            gold_spend = list(self._run_query(gold_sql))[0].total_spend

            delta = abs(bronze_spend - gold_spend)
            passed = delta <= Config.SPEND_TOLERANCE

            status = "PASS" if passed else "FAIL"
            self._log.info(
                "  %-10s bronze=$%s  gold=$%s  Δ=$%.2f  %s",
                f"{platform}:", f"{bronze_spend:,.2f}", f"{gold_spend:,.2f}",
                delta, status,
            )

            if not passed:
                all_passed = False
                msg = f"{platform} spend Δ=${delta:.2f} exceeds tolerance ${Config.SPEND_TOLERANCE}"
                self._critical_failures.append(msg)

        detail = "all platforms within tolerance" if all_passed else "spend mismatch detected"
        self._record(3, "Spend reconciliation", "PASS" if all_passed else "FAIL", detail)

    # ------------------------------------------------------------------
    # Check 4: Per-platform conversion reconciliation
    # ------------------------------------------------------------------
    def _check_conversion_reconciliation(self):
        """Check 4: per-platform conversions match exactly."""
        self._log.info("[4/9] Per-platform conversion reconciliation")

        all_passed = True
        for platform, bronze_table in Config.BRONZE_TABLES.items():
            # Bronze conversions (SAFE_CAST — all-STRING landing tables)
            bronze_sql = f"""
            SELECT COALESCE(SUM(SAFE_CAST(conversions AS INT64)), 0) AS total_conversions
            FROM `{bronze_table}`
            """
            bronze_conv = list(self._run_query(bronze_sql))[0].total_conversions

            # Gold conversions
            gold_sql = f"""
            SELECT COALESCE(SUM(conversions), 0) AS total_conversions
            FROM `{Config.GOLD_TABLE}`
            WHERE platform = '{platform}'
            """
            gold_conv = list(self._run_query(gold_sql))[0].total_conversions

            passed = bronze_conv == gold_conv
            status = "PASS" if passed else "FAIL"
            self._log.info(
                "  %-10s bronze=%d  gold=%d  %s",
                f"{platform}:", bronze_conv, gold_conv, status,
            )

            if not passed:
                all_passed = False
                msg = f"{platform} conversions: bronze={bronze_conv} != gold={gold_conv}"
                self._critical_failures.append(msg)

        detail = "all platforms match exactly" if all_passed else "conversion mismatch detected"
        self._record(4, "Conversion reconciliation", "PASS" if all_passed else "FAIL", detail)

    # ------------------------------------------------------------------
    # Check 5: Null dates in gold
    # ------------------------------------------------------------------
    def _check_null_dates(self):
        """Check 5: no null dates in gold."""
        self._log.info("[5/9] Null dates in gold")

        sql = f"""
        SELECT COUNT(*) AS null_count
        FROM `{Config.GOLD_TABLE}`
        WHERE date IS NULL
        """
        null_count = list(self._run_query(sql))[0].null_count

        if null_count == 0:
            msg = f"{null_count} rows with null date"
            self._log.info("  PASS — %s", msg)
            self._record(5, "Null dates in gold", "PASS", msg)
        else:
            msg = f"{null_count} rows with null date"
            self._log.error("  FAIL — %s", msg)
            self._critical_failures.append(msg)
            self._record(5, "Null dates in gold", "FAIL", msg)

    # ------------------------------------------------------------------
    # Check 6: Negative spend in gold
    # ------------------------------------------------------------------
    def _check_negative_spend(self):
        """Check 6: no negative spend in gold."""
        self._log.info("[6/9] Negative spend in gold")

        sql = f"""
        SELECT COUNT(*) AS neg_count
        FROM `{Config.GOLD_TABLE}`
        WHERE spend < 0
        """
        neg_count = list(self._run_query(sql))[0].neg_count

        if neg_count == 0:
            msg = f"{neg_count} rows with negative spend"
            self._log.info("  PASS — %s", msg)
            self._record(6, "Negative spend in gold", "PASS", msg)
        else:
            msg = f"{neg_count} rows with negative spend"
            self._log.error("  FAIL — %s", msg)
            self._critical_failures.append(msg)
            self._record(6, "Negative spend in gold", "FAIL", msg)

    # ------------------------------------------------------------------
    # Check 7: ML output tables populated
    # ------------------------------------------------------------------
    def _check_ml_tables_populated(self):
        """Check 7: ML output tables exist and have rows (Warning)."""
        self._log.info("[7/9] ML output tables populated")

        all_ok = True
        for table_ref in Config.ML_TABLES:
            table_name = table_ref.split(".")[-1]
            sql = f"SELECT COUNT(*) AS row_count FROM `{table_ref}`"

            try:
                row_count = list(self._run_query(sql))[0].row_count
                passed = row_count > 0
                status = "PASS" if passed else "WARN"
                self._log.info("  %s: %d rows  %s", table_name, row_count, status)

                if not passed:
                    all_ok = False
                    self._warnings.append(f"{table_name} is empty (0 rows)")
            except Exception as e:
                all_ok = False
                self._log.warning("  %s: WARN — query failed (%s)", table_name, e)
                self._warnings.append(f"{table_name} query failed: {e}")

        detail = "all ML tables populated" if all_ok else "one or more ML tables empty or missing"
        self._record(7, "ML tables populated", "PASS" if all_ok else "WARN", detail)

    # ------------------------------------------------------------------
    # Check 8: Latest audit status
    # ------------------------------------------------------------------
    def _check_latest_audit_status(self):
        """Check 8: most recent audit entry is SUCCESS (Warning)."""
        self._log.info("[8/9] Latest audit status")

        sql = f"""
        SELECT status
        FROM `{Config.AUDIT_TABLE}`
        ORDER BY last_updated_at DESC
        LIMIT 1
        """

        try:
            rows = list(self._run_query(sql))
            if not rows:
                msg = "no audit rows found"
                self._log.warning("  WARN — %s", msg)
                self._warnings.append(msg)
                self._record(8, "Latest audit status", "WARN", msg)
                return

            latest_status = rows[0].status
            if latest_status == "SUCCESS":
                msg = f"most recent audit: status={latest_status}"
                self._log.info("  PASS — %s", msg)
                self._record(8, "Latest audit status", "PASS", msg)
            else:
                msg = f"most recent audit: status={latest_status} (expected SUCCESS)"
                self._log.warning("  WARN — %s", msg)
                self._warnings.append(msg)
                self._record(8, "Latest audit status", "WARN", msg)
        except Exception as e:
            msg = f"audit table query failed: {e}"
            self._log.warning("  WARN — %s", msg)
            self._warnings.append(msg)
            self._record(8, "Latest audit status", "WARN", msg)

    # ------------------------------------------------------------------
    # Check 9: Quarantine summary
    # ------------------------------------------------------------------
    def _check_quarantine_count(self):
        """Check 9: informational quarantine row count."""
        self._log.info("[9/9] Quarantine summary")

        sql = f"""
        SELECT COUNT(*) AS total_quarantined
        FROM `{Config.QUARANTINE_TABLE}`
        """

        try:
            total = list(self._run_query(sql))[0].total_quarantined
            msg = f"{total} total quarantine rows"
            self._log.info("  INFO — %s", msg)
            self._info_messages.append(msg)
            self._record(9, "Quarantine summary", "INFO", msg)
        except Exception as e:
            msg = f"quarantine query failed: {e}"
            self._log.warning("  WARN — %s", msg)
            self._warnings.append(msg)
            self._record(9, "Quarantine summary", "WARN", msg)

    # ------------------------------------------------------------------
    # Summary table
    # ------------------------------------------------------------------
    def _print_summary(self):
        """Print a formatted summary table of all 9 check results."""
        self._log.info("")
        self._log.info(
            "  %-5s %-35s %-6s %s", "Check", "Name", "Status", "Detail"
        )
        self._log.info("  %s", "-" * 80)
        for r in self._results:
            self._log.info(
                "  %-5s %-35s %-6s %s",
                f"[{r['check']}]",
                r["name"],
                r["status"],
                r["detail"],
            )
        self._log.info("  %s", "-" * 80)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self) -> int:
        """Execute all 9 checks. Return 0 (pass) or 1 (critical fail)."""
        self._log.info("=" * 72)
        self._log.info("Script 07 — QA Validation Runner  started")
        self._log.info("=" * 72)

        self._check_row_count_reconciliation()
        self._check_column_count()
        self._check_spend_reconciliation()
        self._check_conversion_reconciliation()
        self._check_null_dates()
        self._check_negative_spend()
        self._check_ml_tables_populated()
        self._check_latest_audit_status()
        self._check_quarantine_count()

        # Summary table
        self._print_summary()

        # Final verdict
        critical_count = len(self._critical_failures)
        warning_count = len(self._warnings)
        info_count = len(self._info_messages)
        critical_passed = 6 - critical_count  # 6 critical checks total

        self._log.info("=" * 72)
        if self._critical_failures:
            self._log.error(
                "QA FAILED — %d critical failure(s), %d warning(s), %d informational",
                critical_count, warning_count, info_count,
            )
            for f in self._critical_failures:
                self._log.error("  FAIL: %s", f)
            self._log.info("=" * 72)
            return 1
        else:
            self._log.info(
                "QA PASSED — %d critical checks OK, %d warnings OK, %d informational",
                critical_passed, warning_count, info_count,
            )
            self._log.info("=" * 72)
            return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    runner = QAValidationRunner()
    sys.exit(runner.run())