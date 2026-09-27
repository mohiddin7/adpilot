"""lib.api_client against the real API app through an in-process session, plus the failures a deployed service
produces: a rejected key, a rate limit, a cold start, a broken stream."""

import pytest
import requests
from lib import api_client
from lib.api_client import ApiError, parse_sse

WINDOW = [("date_from", "2024-01-01"), ("date_to", "2024-01-30")]


def test_parse_sse_skips_keepalives_and_needs_no_blank_lines():
    lines = [": keepalive", "event: status", 'data: {"phase": "thinking"}', "event: answer",
             'data: {"answer_md": "x"}', "", "event: done", "data: {}"]
    assert list(parse_sse(lines)) == [("status", {"phase": "thinking"}), ("answer", {"answer_md": "x"}), ("done", {})]


def test_reads_round_trip_through_the_real_app(dash_api):
    ac, _, _ = dash_api
    assert ac.dashboard()["date_max"] == "2024-01-30"
    assert {p["id"] for p in ac.panels("overview", [*WINDOW, ("platform", "Google")])} >= {"kpis", "spend_trend"}
    assert ac.filter_options("deep_dive", WINDOW)["platform"]["values"] == ["Facebook", "Google", "TikTok"]
    assert ac.pacing()["as_of"] == "2024-01-30"


def test_ask_returns_the_answer_body(dash_api):
    ac, _, _ = dash_api
    body = ac.ask("What was spend by platform?", "s1")
    assert "pre-defined query" in body["answer_md"] and body["refused"] is False


def test_ask_stream_yields_status_then_answer_then_done(dash_api):
    ac, _, _ = dash_api
    names = [name for name, _ in ac.ask_stream("What was spend by platform?", "s2")]
    assert names[0] == "status" and names[-2:] == ["answer", "done"]


def test_a_rejected_key_is_an_auth_error(dash_api, monkeypatch):
    from lib import config

    monkeypatch.setattr(config, "api_key", lambda: "x" * 32)
    with pytest.raises(ApiError) as exc:
        api_client.dashboard()
    assert exc.value.kind == "auth"


def test_a_rate_limit_carries_retry_after(dash_api):
    from adpilot.core.guardrails import RateLimiter

    _, client, _ = dash_api
    client.app.state.dash_limiter = RateLimiter(per_minute=0)
    with pytest.raises(ApiError) as exc:
        api_client.dashboard()
    assert exc.value.kind == "rate_limited" and exc.value.retry_after >= 1


def test_a_bad_filter_is_a_bad_request_with_the_servers_reason(dash_api):
    with pytest.raises(ApiError) as exc:
        api_client.panels("overview", [("date_from", "someday"), ("date_to", "2024-01-30")])
    assert exc.value.kind == "bad_request" and "date_from" in exc.value.message


class Flaky:
    """Fails the first `n` requests with a connection error, then hands off to the in-process session."""

    def __init__(self, inner, n):
        self.inner, self.n, self.calls = inner, n, 0

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.n:
            raise requests.ConnectionError("cold start")
        return self.inner.request(*args, **kwargs)


def test_one_connection_failure_is_retried_silently(dash_api, monkeypatch):
    flaky = Flaky(api_client.session, 1)
    monkeypatch.setattr(api_client, "session", flaky)
    assert api_client.dashboard()["date_max"] == "2024-01-30" and flaky.calls == 2


def test_two_connection_failures_are_unavailable(dash_api, monkeypatch):
    monkeypatch.setattr(api_client, "session", Flaky(api_client.session, 2))
    with pytest.raises(ApiError) as exc:
        api_client.dashboard()
    assert exc.value.kind == "unavailable"


def test_an_error_event_in_the_stream_raises(dash_api, monkeypatch):
    class Broken:
        status_code, headers = 200, {}

        def iter_lines(self, decode_unicode=False):
            return iter(["event: status", 'data: {"phase": "thinking"}', "",
                         "event: error", 'data: {"kind": "RuntimeError", "message": "worker died"}', ""])

        def close(self):
            pass

    class Session:
        def request(self, *args, **kwargs):
            return Broken()

    monkeypatch.setattr(api_client, "session", Session())
    with pytest.raises(ApiError, match="worker died"):
        list(api_client.ask_stream("q", "s3"))
