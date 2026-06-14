"""
lib/page_style.py — Shared dashboard CSS, applied once per page.

Why centralize:
  - Equal-height columns (cards line up) require a single CSS rule across pages.
  - KPI font sizes, border styles, scrollbars — all standardised here.
  - Each page just calls inject_page_style() right after st.set_page_config().
"""
from __future__ import annotations

import streamlit as st

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', -apple-system, sans-serif; }

/* ── KPI metric cards (st.metric) ──────────────────────────────────────── */
[data-testid="metric-container"] {
    background: linear-gradient(135deg, #1A1D27 0%, #1E2235 100%);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
    padding: 18px 22px;
    min-height: 110px;
    position: relative;
    overflow: hidden;
}
[data-testid="metric-container"]::before {
    content: '';
    position: absolute; top: 0; left: 0; right: 0;
    height: 3px;
    background: linear-gradient(90deg, #2563EB, #7C3AED);
}
[data-testid="metric-container"] [data-testid="stMetricLabel"] p {
    font-size: 12px !important;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: rgba(255,255,255,0.65) !important;
    font-weight: 500;
}
[data-testid="metric-container"] [data-testid="stMetricValue"] {
    font-size: 30px !important;
    font-weight: 700 !important;
    line-height: 1.2 !important;
}
[data-testid="metric-container"] [data-testid="stMetricDelta"] {
    font-size: 13px !important;
}

/* ── Bordered containers ───────────────────────────────────────────────── */
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 12px !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
    background: #1A1D27 !important;
}
/* Internal padding so content doesn't crash into the border */
[data-testid="stVerticalBlockBorderWrapper"] > div:first-child {
    padding: 14px 18px !important;
}

/* ── Equal-height side-by-side cards ──────────────────────────────────── */
/* When cards live inside columns, make them stretch so they line up. */
div[data-testid="column"] {
    display: flex;
    flex-direction: column;
}
div[data-testid="column"] > div {
    width: 100%;
    flex: 1;
    display: flex;
    flex-direction: column;
}
div[data-testid="column"] > div > [data-testid="stVerticalBlockBorderWrapper"] {
    height: 100%;
}

/* ── Sidebar styling ───────────────────────────────────────────────────── */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0F1117 0%, #141720 100%);
    border-right: 1px solid rgba(255,255,255,0.07);
}

/* ── Headings + dividers ──────────────────────────────────────────────── */
hr { border-color: rgba(255,255,255,0.07) !important; }
h1, h2, h3 { letter-spacing: -0.02em; }

/* ── Custom scrollbar ─────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 3px; }

/* ── Tab styling for cleaner look ─────────────────────────────────────── */
button[data-baseweb="tab"] {
    font-size: 14px !important;
    font-weight: 500 !important;
}
</style>
"""


def inject_page_style() -> None:
    """Inject the shared CSS once. Call at the top of every page."""
    st.markdown(_CSS, unsafe_allow_html=True)