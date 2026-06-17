"""
lib/config.py — single source of truth for all configuration.

Resolution order: st.secrets → env vars → default.
Works identically in local dev (env vars + gcloud ADC) and Streamlit Cloud
(secrets.toml in dashboard).
"""
from __future__ import annotations

import os
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

log = logging.getLogger(__name__)


def _get(section: str, key: str, env_var: str, default: str = "") -> str:
    """Try st.secrets first, then env var, then default."""
    try:
        import streamlit as st  # lazy import — avoids import errors in batch mode
        value = st.secrets[section][key]
        if value:
            return str(value).strip()
    except Exception:
        pass  # not in Streamlit context, or key missing — fall through
    return os.environ.get(env_var, default).strip()


# ── LLM config ──────────────────────────────────────────────────────────────
# Any OpenAI-compatible chat-completions endpoint works. OpenRouter free tier by default.
LLM_ENDPOINT_URL: str = _get("llm", "LLM_ENDPOINT_URL", "LLM_ENDPOINT_URL",
                              "https://openrouter.ai/api/v1/chat/completions")
LLM_BEARER_TOKEN: str = _get("llm", "LLM_BEARER_TOKEN", "LLM_BEARER_TOKEN", "")
LLM_TARGET_MODEL: str = _get("llm", "LLM_TARGET_MODEL", "LLM_TARGET_MODEL",
                              "inclusionai/ling-3.0-flash-vl:free")
# Optional: a second model tried when the primary is rate-limited or unavailable.
LLM_FALLBACK_MODEL: str = _get("llm", "LLM_FALLBACK_MODEL", "LLM_FALLBACK_MODEL",
                                "google/gemma-4-26b-a4b-it:free")

# ── GCP config ──────────────────────────────────────────────────────────────
GCP_PROJECT: str = _get("gcp", "project_id", "BQ_PROJECT_ID", "adpilot-lakehouse")

# Optional: service account JSON for Streamlit Cloud deployments
GCP_SERVICE_ACCOUNT_JSON: str = _get("gcp", "GCP_SERVICE_ACCOUNT_JSON",
                                     "GCP_SERVICE_ACCOUNT_JSON", "")

# ── Dataset references ───────────────────────────────────────────────────────
_PROD_DS = f"{GCP_PROJECT}." + _get("gcp", "production_dataset", "BQ_PRODUCTION_DATASET", "adpilot_production")
_STG_DS  = f"{GCP_PROJECT}." + _get("gcp", "staging_dataset",    "BQ_STAGING_DATASET",    "adpilot_staging")

GOLD_REF     = f"{_PROD_DS}.fct_unified_marketing_performance"
ANOMALY_REF  = f"{_STG_DS}.fct_anomaly_flags"
BUDGET_REF   = f"{_STG_DS}.tbl_budget_recommendations"
FORECAST_REF = f"{_STG_DS}.tbl_forecast"
AUDIT_REF    = f"{_STG_DS}.tbl_ingestion_audit"
QUARANTINE_REF = f"{_STG_DS}.stg_quarantine_logs"

# Allowlist — chatbot SQL agent may ONLY reference these tables
ALLOWED_TABLES: set[str] = {GOLD_REF, ANOMALY_REF, BUDGET_REF, FORECAST_REF}

# ── Cost guardrails ──────────────────────────────────────────────────────────
# Per-query byte caps (BigQuery rejects before billing if exceeded)
MAX_BYTES_OVERVIEW   = 500  * 1024 * 1024   # 500 MB  — page queries
MAX_BYTES_INSIGHTS   = 200  * 1024 * 1024   # 200 MB  — insight context queries
MAX_BYTES_CHAT       =  10  * 1024 * 1024   #  10 MB  — per chat turn
MAX_BYTES_DEFAULT    = 100  * 1024 * 1024   # 100 MB  — generic fallback

# Result-set cap for chatbot
MAX_RESULT_ROWS = 100

# ── Brand colours (Plotly + CSS variables in pages) ──────────────────────────
PLATFORM_COLORS: dict[str, str] = {
    "Facebook": "#1877F2",
    "Google":   "#34A853",
    "TikTok":   "#FE2C55",
}

# ── Ground-truth checkpoints (for QA assertions) ─────────────────────────────
GROUND_TRUTH = {
    "total_spend":       130_244.90,
    "total_conversions": 13_363,
    "blended_cpa":       9.75,
    "facebook_spend":    18_292.00,
    "google_spend":      37_686.20,
    "tiktok_spend":      74_266.70,
}