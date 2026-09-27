"""Shared dashboard CSS, applied once per page. Colours come from lib.theme (assets/palette.json); the base theme is
.streamlit/config.toml at the repo root."""

from __future__ import annotations

import streamlit as st

from .theme import COLORS, GROUNDS

_CSS = f"""
<style>
[data-testid="stMetric"] {{ background: {GROUNDS["surface"]}; border-top: 3px solid {COLORS["brand"]}; }}
[data-testid="stMetricLabel"] p {{ font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
</style>
"""


def inject_page_style() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
