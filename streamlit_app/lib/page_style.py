"""Shared dashboard CSS, applied once per page. Colours come from lib.theme (assets/palette.json); the base theme is
.streamlit/config.toml at the repo root."""

from __future__ import annotations

import streamlit as st

from .theme import ACCOUNT, COLORS, GRID, GROUNDS, MUTED

GROUNDS_BG = GROUNDS["bg"]

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
/* The header and the filter bar stay on top while scrolling. Selectors are Streamlit's st-key-<key> classes and its
   stLayoutWrapper (1.64): look again after an upgrade. --ad-top clears Streamlit's own top bar; tune it if the
   screenshots show a gap or an overlap. */
:root {{ --ad-top: 3.75rem; --ad-header: 3rem; }}
.stVerticalBlock.st-key-ad_header {{ height: var(--ad-header); overflow: hidden; }}
.stVerticalBlock.st-key-ad_header, .stVerticalBlock.st-key-sticky_filters, [data-testid="stLayoutWrapper"]:has(> .st-key-ad_header),
[data-testid="stLayoutWrapper"]:has(> .st-key-sticky_filters) {{ flex: none; }}
.st-key-ad_header, [data-testid="stLayoutWrapper"]:has(> .st-key-ad_header) {{
  position: sticky; top: var(--ad-top); z-index: 990; background: {GROUNDS_BG}; }}
.st-key-sticky_filters, [data-testid="stLayoutWrapper"]:has(> .st-key-sticky_filters) {{
  position: sticky; top: calc(var(--ad-top) + var(--ad-header)); z-index: 980; background: {GROUNDS_BG}; }}
.ad-header {{ display: flex; align-items: baseline; gap: .75rem; height: var(--ad-header); border-bottom: 1px solid {GRID}; }}
.ad-brand {{ font-weight: 700; font-size: 1.35rem; color: {ACCOUNT}; letter-spacing: -.01em; }}
.ad-page {{ font-size: 1.35rem; color: {ACCOUNT}; }}
.ad-asof {{ margin-left: auto; color: {MUTED}; font-size: .875rem; }}
</style>
"""


def inject_page_style() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
