"""
lib/bq_client.py — Cached, read-only BigQuery wrapper.

Design guarantees:
  - Single Client instance per Streamlit session (cache_resource).
  - All query results cached for TTL seconds (cache_data, default 5 min).
  - Every job has a maximum_bytes_billed cap — fail loud, never silent overrun.
  - Returns pd.DataFrame or raises — never raw row iterators.
  - Service-account JSON supported for Streamlit Cloud deployments.
  - Handles both ADC (local) and service-account JSON (cloud) auth transparently.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import pandas as pd
import streamlit as st
from google.cloud import bigquery
from google.oauth2 import service_account

from . import config

log = logging.getLogger(__name__)


# ── Client (one instance per app server process) ─────────────────────────────

@st.cache_resource(show_spinner=False)
def get_client() -> bigquery.Client:
    """
    Return a BigQuery client.
    Auth strategy:
      1. If GCP_SERVICE_ACCOUNT_JSON is set (Streamlit Cloud), use it.
      2. Otherwise, rely on Application Default Credentials (local dev / Cloud Run).
    """
    if config.GCP_SERVICE_ACCOUNT_JSON:
        try:
            sa_info = json.loads(config.GCP_SERVICE_ACCOUNT_JSON)
            credentials = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            client = bigquery.Client(
                project=config.GCP_PROJECT,
                credentials=credentials,
            )
            log.info("BQ client: service-account auth")
            return client
        except Exception as exc:
            log.warning("Service-account JSON parse failed (%s); falling back to ADC", exc)

    client = bigquery.Client(project=config.GCP_PROJECT)
    log.info("BQ client: ADC auth, project=%s", config.GCP_PROJECT)
    return client


# ── Query runner ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def run_query(sql: str,
              max_bytes: int = config.MAX_BYTES_DEFAULT,
              timeout_seconds: float = 60.0) -> pd.DataFrame:
    """
    Execute a BigQuery SQL string and return a DataFrame.

    Args:
        sql:             BigQuery Standard SQL string (SELECT only).
        max_bytes:       Maximum bytes this job may scan before being rejected.
        timeout_seconds: API timeout per attempt.

    Returns:
        pd.DataFrame — empty if no rows, never None.

    Raises:
        google.api_core.exceptions.GoogleAPIError on failure.
    """
    client = get_client()
    job_config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes)

    log.debug("BQ query (max_bytes=%d): %s", max_bytes, sql[:300])
    job = client.query(sql, job_config=job_config, timeout=timeout_seconds)
    df = job.to_dataframe(create_bqstorage_client=False)
    log.debug("BQ result: %d rows × %d cols", len(df), len(df.columns))
    return df


def run_query_uncached(sql: str,
                       max_bytes: int = config.MAX_BYTES_DEFAULT,
                       timeout_seconds: float = 60.0) -> pd.DataFrame:
    """
    Same as run_query but NOT cached.
    Use for chat-turn queries (every question is unique; caching wastes memory).

    Raises:
      BQUserFriendlyError when the query fails — message is safe to show users.
    """
    client = get_client()
    job_config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes)
    try:
        job = client.query(sql, job_config=job_config, timeout=timeout_seconds)
        return job.to_dataframe(create_bqstorage_client=False)
    except Exception as exc:
        # Translate into something safe to display
        raise _translate_bq_error(exc, max_bytes) from exc


class BQUserFriendlyError(Exception):
    """A BigQuery error whose message is safe to show to end users.

    Attributes:
      friendly: short message displayed in the UI
      technical: full underlying error (logged but not displayed)
      hint:     optional next-step suggestion
    """

    def __init__(self, friendly: str, technical: str = "", hint: str = ""):
        super().__init__(friendly)
        self.friendly  = friendly
        self.technical = technical
        self.hint      = hint


def _translate_bq_error(exc: Exception, max_bytes: int) -> BQUserFriendlyError:
    """Convert a raw BQ exception into a user-friendly one. Logs the original."""
    raw = str(exc)
    log.error("BQ error (raw): %s", raw[:1000])
    raw_lower = raw.lower()

    # 1. Cost cap exceeded
    if "byteslimit" in raw_lower or "bytes billed" in raw_lower or "bytesbilledlimit" in raw_lower:
        mb = max_bytes // (1024 * 1024)
        return BQUserFriendlyError(
            friendly=(
                "That question requires scanning more data than the per-query budget "
                f"({mb} MB) allows. "
            ),
            technical=raw,
            hint=(
                "Try narrowing the question — e.g. add a date range, focus on one "
                "platform, or skip the campaign-level join."
            ),
        )

    # 2. Permission denied
    if "access denied" in raw_lower or "permission denied" in raw_lower or "403" in raw:
        return BQUserFriendlyError(
            friendly="I don't have permission to read that data.",
            technical=raw,
            hint="The service account may need additional dataViewer permissions.",
        )

    # 3. Table not found
    if "not found" in raw_lower and "table" in raw_lower:
        return BQUserFriendlyError(
            friendly="A required table is missing.",
            technical=raw,
            hint="The data pipeline may not have run yet. Try refreshing in a moment.",
        )

    # 4. Bad query (syntax / column)
    if "syntax error" in raw_lower or "invalidquery" in raw_lower or "400" in raw[:5]:
        return BQUserFriendlyError(
            friendly="The generated query had a syntax issue.",
            technical=raw,
            hint="Try rephrasing your question with simpler wording.",
        )

    # 5. Timeout / deadline
    if "deadline" in raw_lower or "timeout" in raw_lower:
        return BQUserFriendlyError(
            friendly="The query took too long to complete.",
            technical=raw,
            hint="Try a more specific question that requires less data.",
        )

    # 6. Rate limit
    if "rate limit" in raw_lower or "quota" in raw_lower or "429" in raw[:5]:
        return BQUserFriendlyError(
            friendly="Too many queries in a short period.",
            technical=raw,
            hint="Wait a few seconds and try again.",
        )

    # Default fallback
    return BQUserFriendlyError(
        friendly="Something went wrong running that query.",
        technical=raw,
        hint="Try a different phrasing of your question.",
    )


# ── Schema discovery (used by self-healing SQL to feed real column names back) ──

def discover_table_schema(table_ref: str) -> Optional[str]:
    """
    Return column names and types for a fully-qualified table reference.
    Uses client.get_table() — no query job, no Storage API, works with
    dataset-scoped dataViewer.
    Returns "col1 (TYPE), col2 (TYPE), ..." or None if inaccessible.
    """
    try:
        table = get_client().get_table(table_ref)
        if not table.schema:
            return None
        return ", ".join(
            f"{field.name} ({field.field_type})" for field in table.schema
        )
    except Exception as exc:
        log.warning("Schema discovery failed for %s: %s", table_ref, exc)
        return None


def discover_schemas_from_sql(sql: str) -> str:
    """
    Extract all backtick-quoted `project.dataset.table` references from a
    SQL string, call discover_table_schema() for each, and return a
    formatted block suitable for embedding in a repair prompt.

    Example output:
        ACTUAL SCHEMA — `proj.dataset.tbl_forecast`:
          forecast_execution_date (DATE), target_date (DATE), platform (STRING),
          metric_name (STRING), predicted_value (FLOAT64), lower_bound (FLOAT64),
          upper_bound (FLOAT64), model_used (STRING)
    """
    import re as _re
    tables = list(dict.fromkeys(_re.findall(r"`([^`]+\.[^`]+\.[^`]+)`", sql)))
    if not tables:
        return ""
    blocks = []
    for ref in tables:
        schema = discover_table_schema(ref)
        if schema:
            blocks.append(f"ACTUAL SCHEMA — `{ref}`:\n  {schema}")
    return "\n\n".join(blocks)

@st.cache_data(ttl=120, show_spinner=False)
def table_exists(full_table_id: str) -> bool:
    """Return True if the table exists and is accessible."""
    try:
        get_client().get_table(full_table_id)
        return True
    except Exception:
        return False


# ── Scalar helper ─────────────────────────────────────────────────────────────

def scalar(sql: str, max_bytes: int = config.MAX_BYTES_DEFAULT,
           default: Any = None) -> Any:
    """Run a query that returns a single value; return default on failure."""
    try:
        df = run_query(sql, max_bytes=max_bytes)
        if df.empty or df.shape[1] == 0:
            return default
        return df.iloc[0, 0]
    except Exception as exc:
        log.warning("BQ scalar failed: %s | sql: %s", exc, sql[:200])
        return default


# ── Pre-baked KPI fetchers ────────────────────────────────────────────────────
# These centralize the "totals from gold" queries so no page hardcodes numbers.

@st.cache_data(ttl=300, show_spinner=False)
def fetch_top_kpis() -> dict:
    """
    Return current period top-line KPIs from the gold mart.
    Dynamically determines the date range from the data itself — no hardcoding.

    Returns dict with: total_spend, total_conversions, total_impressions,
    total_clicks, blended_cpa, blended_ctr, start_date, end_date,
    n_platforms, n_campaigns.
    """
    sql = f"""
        SELECT
            MIN(date)                                  AS start_date,
            MAX(date)                                  AS end_date,
            COUNT(DISTINCT platform)                   AS n_platforms,
            COUNT(DISTINCT campaign_id)                AS n_campaigns,
            SUM(spend)                                 AS total_spend,
            SUM(conversions)                           AS total_conversions,
            SUM(impressions)                           AS total_impressions,
            SUM(clicks)                                AS total_clicks,
            SAFE_DIVIDE(SUM(spend), SUM(conversions))  AS blended_cpa,
            SAFE_DIVIDE(SUM(clicks), SUM(impressions)) AS blended_ctr
        FROM `{config.GOLD_REF}`
    """
    df = run_query(sql, max_bytes=config.MAX_BYTES_OVERVIEW)
    if df.empty:
        return {}
    row = df.iloc[0].to_dict()
    return row


@st.cache_data(ttl=300, show_spinner=False)
def fetch_platform_summary() -> "pd.DataFrame":
    """Platform-level rollup from the gold mart."""
    sql = f"""
        SELECT
            platform,
            ROUND(SUM(spend), 2)                       AS spend,
            SUM(conversions)                            AS conversions,
            SUM(impressions)                            AS impressions,
            SUM(clicks)                                 AS clicks,
            ROUND(SAFE_DIVIDE(SUM(spend), SUM(conversions)), 2) AS cpa,
            ROUND(SAFE_DIVIDE(SUM(clicks), SUM(impressions)), 4) AS ctr,
            ROUND(SAFE_DIVIDE(SUM(spend), SUM(clicks)), 2) AS cpc,
            ROUND(SAFE_DIVIDE(SUM(spend), SUM(impressions)) * 1000, 2) AS cpm
        FROM `{config.GOLD_REF}`
        GROUP BY platform
        ORDER BY spend DESC
    """
    return run_query(sql, max_bytes=config.MAX_BYTES_OVERVIEW)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_period_over_period() -> dict:
    """
    Compute period-over-period deltas by splitting the available date range in
    half. Returns {metric: pct_change} for spend, conversions, cpa, ctr.
    """
    sql = f"""
        WITH bounds AS (
          SELECT MIN(date) AS start_d, MAX(date) AS end_d
          FROM `{config.GOLD_REF}`
        ),
        windows AS (
          SELECT
            start_d,
            end_d,
            DATE_ADD(start_d, INTERVAL CAST(DATE_DIFF(end_d, start_d, DAY) / 2 AS INT64) DAY) AS mid_d
          FROM bounds
        )
        SELECT
            -- Recent half
            SUM(IF(g.date > w.mid_d, g.spend, 0))        AS recent_spend,
            SUM(IF(g.date > w.mid_d, g.conversions, 0))  AS recent_conv,
            SUM(IF(g.date > w.mid_d, g.clicks, 0))       AS recent_clicks,
            SUM(IF(g.date > w.mid_d, g.impressions, 0))  AS recent_impr,
            -- Earlier half
            SUM(IF(g.date <= w.mid_d, g.spend, 0))       AS prior_spend,
            SUM(IF(g.date <= w.mid_d, g.conversions, 0)) AS prior_conv,
            SUM(IF(g.date <= w.mid_d, g.clicks, 0))      AS prior_clicks,
            SUM(IF(g.date <= w.mid_d, g.impressions, 0)) AS prior_impr
        FROM `{config.GOLD_REF}` g
        CROSS JOIN windows w
    """
    df = run_query(sql, max_bytes=config.MAX_BYTES_OVERVIEW)
    if df.empty:
        return {}

    r = df.iloc[0].to_dict()
    def pct(new, old):
        return (new - old) / old if old else None

    recent_cpa = (r["recent_spend"] / r["recent_conv"]) if r["recent_conv"] else None
    prior_cpa  = (r["prior_spend"]  / r["prior_conv"])  if r["prior_conv"]  else None
    recent_ctr = (r["recent_clicks"] / r["recent_impr"]) if r["recent_impr"] else None
    prior_ctr  = (r["prior_clicks"]  / r["prior_impr"])  if r["prior_impr"]  else None

    return {
        "spend_delta":       pct(r["recent_spend"], r["prior_spend"]),
        "conversions_delta": pct(r["recent_conv"],  r["prior_conv"]),
        "cpa_delta":         pct(recent_cpa, prior_cpa) if recent_cpa and prior_cpa else None,
        "ctr_delta":         pct(recent_ctr, prior_ctr) if recent_ctr and prior_ctr else None,
    }