"""Dashboard configuration: where adpilot-api lives and the key to call it. Nothing else — the dashboard holds no
database or model credentials.

Resolution order: st.secrets["api"] → environment (the repo's .env for local runs) → default.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _get(section: str, key: str, env_var: str, default: str = "") -> str:
    try:
        import streamlit as st

        value = st.secrets[section][key]
        if value:
            return str(value).strip()
    except Exception:  # no secrets file, no such section, or not running under Streamlit
        pass
    return os.environ.get(env_var, default).strip()


# Functions, not constants, so a test (or a secrets edit picked up on rerun) is read at call time.
def api_url() -> str:
    return _get("api", "ADPILOT_API_URL", "ADPILOT_API_URL", "http://localhost:8080").rstrip("/")


def api_key() -> str:
    return _get("api", "ADPILOT_API_KEY", "ADPILOT_API_KEY")
