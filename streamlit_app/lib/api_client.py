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
    return _json(_request("GET", "/dashboard"))


def filter_options(page: str, params: list[tuple[str, str]]) -> dict:
    return _json(_request("GET", "/filters", params=[("page", page), *params]))["options"]


def panels(page: str, params: list[tuple[str, str]]) -> list[dict]:
    return _json(_request("GET", "/panels", params=[("page", page), *params]))["panels"]


def pacing() -> dict:
    return _json(_request("GET", "/pacing"))


def ask(question: str, session_id: str) -> dict:
    # retry_all=False: a POST that reached the server may already have started the model. Retrying it blind
    # could run the question twice, so only a connection that never got there (ConnectTimeout) is retried.
    return _json(_request("POST", "/ask", retry_all=False, json={"question": question, "session_id": session_id}))


def ask_stream(question: str, session_id: str) -> Iterator[tuple[str, dict]]:
    """(event, data) pairs from /ask/stream: `status`… then `answer`, then `done`. An `error` event raises."""
    r = _request("GET", "/ask/stream", retry_all=False,
                 params={"question": question, "session_id": session_id}, stream=True)
    try:
        for name, payload in parse_sse(r.iter_lines(decode_unicode=True)):
            if name == "error":
                # Never surface the server's raw error message (payload["message"]) to viewers — it can carry
                # internal exception text. ApiError.message is shown on the page as-is.
                raise ApiError("server", "The answer stream failed. Try again.")
            yield name, payload
            if name == "done":
                return
    except (requests.RequestException, ValueError) as exc:  # the connection dropped or a line wasn't JSON
        raise ApiError("unavailable", "The answer stream was cut off. Try again.") from exc
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


def _request(method: str, path: str, retry_all: bool = True, **kwargs):
    """retry_all=False (ask/ask_stream): a request that reached the server may have started the model, so only
    a ConnectTimeout (never sent) is retried — never a status code, never a ReadTimeout/other connection drop."""
    key = config.api_key()
    if not key:
        raise ApiError("auth", "The dashboard has no API key configured.")
    url = config.api_url() + path
    headers = {"X-API-Key": key}
    for attempt in (1, 2):
        try:
            r = session.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:  # never show exc: its text carries the service URL
            retryable = isinstance(exc, (requests.ConnectionError, requests.Timeout) if retry_all
                                   else requests.ConnectTimeout)
            if attempt == 2 or not retryable:
                raise ApiError("unavailable", "The AdPilot service did not respond. Try again in a minute.") from exc
        else:
            if not retry_all or r.status_code not in RETRYABLE_STATUS or attempt == 2:
                try:
                    _raise_for(r)
                except ApiError:
                    r.close()  # a streamed response holds its connection until closed
                    raise
                return r
        time.sleep(RETRY_WAIT_S)
    raise ApiError("unavailable", "The AdPilot service did not respond. Try again in a minute.")


def _json(r):
    try:
        return r.json()
    except ValueError as exc:  # a proxy's HTML error page, a truncated body
        raise ApiError("server", "The AdPilot service sent an unreadable response.") from exc


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
