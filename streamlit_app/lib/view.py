"""What every page does first, the error banners, and the cached API reads the pages share."""

from __future__ import annotations

import streamlit as st

from . import api_client
from .api_client import ApiError
from .page_style import inject_page_style


def start(title: str, icon: str) -> None:
    st.set_page_config(page_title=f"{title} · AdPilot", page_icon=icon, layout="wide")
    inject_page_style()


def show_error(exc: ApiError) -> None:
    if exc.kind == "auth":
        st.error("The dashboard can't reach AdPilot: its API key was rejected. "
                 "The owner needs to update the dashboard's [api] secrets.")
    elif exc.kind == "rate_limited":
        st.warning(f"AdPilot is busy. Try again in {exc.retry_after} seconds.")
    elif exc.kind == "unavailable":
        st.warning("AdPilot didn't respond (it may be starting up). Try again in a minute.")
    else:
        st.error(exc.message)


def guarded(fn, *args):
    """An API read the page cannot draw without: on failure, say why and stop the page."""
    try:
        return fn(*args)
    except ApiError as exc:
        show_error(exc)
        st.stop()


# Short client-side caches: every widget click reruns the page, and the API caches for 15 minutes anyway.
# Exceptions are never cached, so a failed read is retried on the next rerun.
@st.cache_data(ttl=300, show_spinner=False)
def meta() -> dict:
    return api_client.dashboard()


@st.cache_data(ttl=300, show_spinner=False)
def options(page: str, params: tuple[tuple[str, str], ...]) -> dict:
    return api_client.filter_options(page, list(params))


@st.cache_data(ttl=300, show_spinner=False)
def panels(page: str, params: tuple[tuple[str, str], ...]) -> list[dict]:
    return api_client.panels(page, list(params))


@st.cache_data(ttl=300, show_spinner=False)
def pacing() -> dict:
    return api_client.pacing()
