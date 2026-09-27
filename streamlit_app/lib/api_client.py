"""The dashboard's only door to data. Every number and every answer comes from adpilot-api over HTTPS; this app
holds no database credentials and no model key."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator

import requests

from . import config

TIMEOUT = (10, 180)  # (connect, read) seconds: a free-model answer can take a couple of minutes
RETRY_WAIT_S = 3.0  # one silent retry: a scale-to-zero Cloud Run instance can take seconds to wake
RETRYABLE_STATUS = {502, 503, 504}

session = requests.Session()  # tests swap in an in-process session (tests/conftest.py)


class ApiError(Exception):
    """kind: auth | rate_limited | bad_request | unavailable | server — the pages word each one differently."""

    def __init__(self, kind: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.kind, self.message, self.retry_after = kind, message, retry_after


def dashboard() -> dict:
    return _request("GET", "/dashboard").json()


def filter_options(page: str, params: list[tuple[str, str]]) -> dict:
    return _request("GET", "/filters", params=[("page", page), *params]).json()["options"]


def panels(page: str, params: list[tuple[str, str]]) -> list[dict]:
    return _request("GET", "/panels", params=[("page", page), *params]).json()["panels"]


def pacing() -> dict:
    return _request("GET", "/pacing").json()


def ask(question: str, session_id: str) -> dict:
    return _request("POST", "/ask", json={"question": question, "session_id": session_id}).json()


def ask_stream(question: str, session_id: str) -> Iterator[tuple[str, dict]]:
    """(event, data) pairs from /ask/stream: `status`… then `answer`, then `done`. An `error` event raises."""
    r = _request("GET", "/ask/stream", params={"question": question, "session_id": session_id}, stream=True)
    try:
        for name, payload in parse_sse(r.iter_lines(decode_unicode=True)):
            if name == "error":
                raise ApiError("server", payload.get("message") or "The answer stream failed.")
            yield name, payload
            if name == "done":
                return
    finally:
        r.close()


def parse_sse(lines: Iterable[str | bytes]) -> Iterator[tuple[str, dict]]:
    """Server-sent events → (event, data). Comments (`: keepalive`) are skipped; a blank line or the next `event:`
    ends an event, so it works whether or not the line iterator keeps blank lines."""
    event, data = None, []
    for raw in lines:
        line = raw.decode() if isinstance(raw, bytes) else raw
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            if event is not None and data:
                yield event, json.loads("\n".join(data))
            event, data = line[len("event:"):].strip(), []
        elif line.startswith("data:"):
            data.append(line[len("data:"):].strip())
        elif line == "" and event is not None and data:
            yield event, json.loads("\n".join(data))
            event, data = None, []
    if event is not None and data:
        yield event, json.loads("\n".join(data))


def _request(method: str, path: str, **kwargs):
    url = config.api_url() + path
    headers = {"X-API-Key": config.api_key()}
    for attempt in (1, 2):
        try:
            r = session.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 2:
                raise ApiError("unavailable", "The AdPilot service did not respond. Try again in a minute.") from exc
        else:
            if r.status_code not in RETRYABLE_STATUS or attempt == 2:
                _raise_for(r)
                return r
        time.sleep(RETRY_WAIT_S)
    raise ApiError("unavailable", "The AdPilot service did not respond. Try again in a minute.")


def _raise_for(r) -> None:
    if r.status_code < 400:
        return
    if r.status_code == 401:
        raise ApiError("auth", "The dashboard's API key was rejected.")
    if r.status_code == 429:
        raise ApiError("rate_limited", "The AdPilot service is busy.", retry_after=_int(r.headers.get("Retry-After"), 5))
    if r.status_code in (404, 422):
        raise ApiError("bad_request", _detail(r))
    raise ApiError("server", f"The AdPilot service failed (HTTP {r.status_code}).")


def _detail(r) -> str:
    try:
        detail = r.json().get("detail")
    except ValueError:
        return f"HTTP {r.status_code}"
    return detail if isinstance(detail, str) else json.dumps(detail)[:300]


def _int(value: str | None, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default
