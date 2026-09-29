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
    assert {p["id"] for p in ac.panels("overview", [*WINDOW, ("platform", "Google")])} >= {"kpis", "kpi_daily"}
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
    """Fails the first `n` requests with `exc` (a connection error by default), then hands off to the
    in-process session."""

    def __init__(self, inner, n, exc=None):
        self.inner, self.n, self.calls = inner, n, 0
        self.exc = exc if exc is not None else requests.ConnectionError("cold start")

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.n:
            raise self.exc
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


def test_a_read_timeout_on_ask_is_not_retried(dash_api, monkeypatch):
    """A ReadTimeout means the request reached the server — it may have already started the model, so ask()
    must not retry it blind."""
    flaky = Flaky(api_client.session, 1, requests.ReadTimeout("stalled"))
    monkeypatch.setattr(api_client, "session", flaky)
    with pytest.raises(ApiError) as exc:
        api_client.ask("What was spend by platform?", "s4")
    assert exc.value.kind == "unavailable" and flaky.calls == 1


def test_a_connect_timeout_on_ask_is_retried_once(dash_api, monkeypatch):
    """A ConnectTimeout means the request never reached the server — safe to retry even for ask()."""
    flaky = Flaky(api_client.session, 1, requests.ConnectTimeout("cold start"))
    monkeypatch.setattr(api_client, "session", flaky)
    body = api_client.ask("What was spend by platform?", "s5")
    assert "pre-defined query" in body["answer_md"] and flaky.calls == 2


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
    # Security: the server's raw error detail ("worker died") must never reach the page — only the generic
    # sentence does.
    with pytest.raises(ApiError, match="The answer stream failed. Try again.") as exc:
        list(api_client.ask_stream("q", "s3"))
    assert "worker died" not in str(exc.value)


class Canned:
    """A session that returns one canned response, whatever is asked."""

    def __init__(self, response):
        self.response, self.calls = response, 0

    def request(self, *args, **kwargs):
        self.calls += 1
        return self.response


HOST = "adpilot-api-abc123.a.run.app"


@pytest.mark.parametrize("exc", [requests.ReadTimeout(f"HTTPSConnectionPool(host='{HOST}'): Read timed out."),
                                 requests.exceptions.ChunkedEncodingError(f"Connection to {HOST} broken")])
def test_a_stream_cut_off_midway_is_unavailable_and_hides_the_host(monkeypatch, exc):
    class Dropping:
        status_code, headers = 200, {}

        def iter_lines(self, decode_unicode=False):
            yield "event: status"
            yield 'data: {"phase": "thinking"}'
            yield ""
            raise exc

        def close(self):
            pass

    monkeypatch.setattr(api_client, "session", Canned(Dropping()))
    monkeypatch.setattr(api_client.config, "api_key", lambda: "k" * 32)
    with pytest.raises(ApiError) as err:
        list(api_client.ask_stream("q", "s"))
    assert err.value.kind == "unavailable" and HOST not in err.value.message


def test_a_stream_with_bad_json_is_unavailable(monkeypatch):
    class Garbled:
        status_code, headers = 200, {}

        def iter_lines(self, decode_unicode=False):
            return iter(["event: status", "data: {not json", ""])

        def close(self):
            pass

    monkeypatch.setattr(api_client, "session", Canned(Garbled()))
    monkeypatch.setattr(api_client.config, "api_key", lambda: "k" * 32)
    with pytest.raises(ApiError) as err:
        list(api_client.ask_stream("q", "s"))
    assert err.value.kind == "unavailable"


class NotJson:
    status_code, headers = 200, {}

    def json(self):
        raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)


@pytest.mark.parametrize("call", [lambda: api_client.dashboard(), lambda: api_client.ask("q", "s"),
                                  lambda: api_client.panels("overview", WINDOW)])
def test_a_200_that_is_not_json_is_a_server_error(monkeypatch, call):
    monkeypatch.setattr(api_client, "session", Canned(NotJson()))
    monkeypatch.setattr(api_client.config, "api_key", lambda: "k" * 32)
    with pytest.raises(ApiError) as err:
        call()
    assert err.value.kind == "server" and err.value.message == "The AdPilot service sent an unreadable response."


def test_a_malformed_api_url_is_unavailable_without_the_url(monkeypatch):
    monkeypatch.setattr(api_client.config, "api_url", lambda: HOST)  # no scheme: requests raises MissingSchema
    monkeypatch.setattr(api_client.config, "api_key", lambda: "k" * 32)
    monkeypatch.setattr(api_client, "RETRY_WAIT_S", 0)
    with pytest.raises(ApiError) as err:
        api_client.dashboard()
    assert err.value.kind == "unavailable" and HOST not in err.value.message


def test_a_rejected_stream_is_closed(monkeypatch):
    class Rejected:
        status_code, headers, closed = 401, {}, False

        def close(self):
            self.closed = True

    rejected = Rejected()
    monkeypatch.setattr(api_client, "session", Canned(rejected))
    monkeypatch.setattr(api_client.config, "api_key", lambda: "k" * 32)
    with pytest.raises(ApiError):
        list(api_client.ask_stream("q", "s"))
    assert rejected.closed


def test_an_empty_api_key_is_an_auth_error_without_a_request(monkeypatch):
    session = Canned(NotJson())
    monkeypatch.setattr(api_client, "session", session)
    monkeypatch.setattr(api_client.config, "api_key", lambda: "")
    with pytest.raises(ApiError) as err:
        api_client.dashboard()
    assert err.value.kind == "auth" and session.calls == 0


def test_insights_round_trips_through_the_real_app(dash_api):
    ac, _, _ = dash_api
    out = ac.insights([("date_from", "2024-01-16"), ("date_to", "2024-01-30")])
    assert set(out) >= {"cards", "checked", "problems"}
