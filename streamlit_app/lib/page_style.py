"""Shared dashboard CSS, applied once per page. Colours come from lib.theme (assets/palette.json); the base theme is
.streamlit/config.toml at the repo root."""

from __future__ import annotations

import streamlit as st

from .theme import COLORS, GROUNDS

_CSS = f"""
<style>
[data-testid="stMetric"] {{ background: {GROUNDS["surface"]}; border-top: 3px solid {COLORS["brand"]}; }}
/* These selectors are Streamlit's own data-testid names (1.64): look at the tiles again after a Streamlit upgrade. */
/* A tile never truncates. Streamlit's own style is one line with an ellipsis; here the label wraps (two lines are
   reserved, so the values line up) and both sizes follow the tile's width (cqw), not the window's. */
[data-testid="stMetric"] {{ container-type: inline-size; }}
[data-testid="stMetric"] [data-testid="stMarkdownContainer"],
[data-testid="stMetric"] [data-testid="stMarkdownContainer"] p {{ white-space: normal; overflow: visible; text-overflow: clip; }}
[data-testid="stMetricLabel"] [data-testid="stMarkdownContainer"] {{ font-size: clamp(9px, 7.7cqw, 12px); min-height: 2.6em; }}
[data-testid="stMetricLabel"] p {{ font-size: inherit; line-height: 1.3; text-transform: uppercase; letter-spacing: .03em; }}
[data-testid="stMetric"] [data-testid="stMetricValue"] {{ font-size: clamp(1rem, 19cqw, 2.25rem); }}
[data-testid="stMetric"] [data-testid="stMetricValue"] p {{ white-space: nowrap; }}
</style>
"""


def inject_page_style() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
