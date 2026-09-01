"""Agent model chain configuration: names, defaults and the legacy-name warning."""

import logging

import pytest
from pydantic_ai import Agent, ModelResponse, TextPart, UnexpectedModelBehavior
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models.function import FunctionModel

from adpilot.core import models as m

AGENT_ENV = ("AGENT_LLM_BEARER_TOKEN", "AGENT_LLM_MODELS")
LEGACY_ENV = ("OPENROUTER_API_KEY", "LLM_BEARER_TOKEN", "LLM_TARGET_MODEL", "LLM_FALLBACK_MODEL", "AGENT_LLM_TARGET_MODEL", "AGENT_LLM_FALLBACK_MODEL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in AGENT_ENV + LEGACY_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(m, "_now", lambda: NOW)  # CAP's reset is 12 h away; the daily-cap rule reads the clock


def test_defaults_are_the_measured_chain():
    # Live probe of every tool-capable free model, 2026-09-23 (docs/models.md); openrouter/free is the last resort.
    assert m.DEFAULT_AGENT_CHAIN == (
        "inclusionai/ling-3.0-flash-fin:free", "inclusionai/ling-3.0-flash-sante:free",
        "nvidia/nemotron-3.5-lightning:free", "openrouter/free",
    )


def test_api_key_reads_the_agent_name(monkeypatch):
    assert m.api_key_from_env() is None
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "k")
    assert m.api_key_from_env() == "k"


def test_legacy_names_are_ignored_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("OPENROUTER_API_KEY", "old")
    with caplog.at_level(logging.WARNING):
        assert m.api_key_from_env() is None
    assert "AGENT_LLM_BEARER_TOKEN" in caplog.text and "OPENROUTER_API_KEY" in caplog.text


def test_legacy_name_beside_the_new_one_warns_but_uses_the_new_one(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "new")
    monkeypatch.setenv("LLM_BEARER_TOKEN", "old")
    with caplog.at_level(logging.WARNING):
        assert m.api_key_from_env() == "new"
    assert "LLM_BEARER_TOKEN" in caplog.text


def test_the_env_list_overrides_the_default_chain(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "k")
    monkeypatch.setenv("AGENT_LLM_MODELS", "vendor/primary, vendor/fallback")
    assert [x.model_name for x in m.build_model().models] == ["vendor/primary", "vendor/fallback"]


def test_one_model_means_no_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "k")
    monkeypatch.setenv("AGENT_LLM_MODELS", "vendor/only")
    model = m.build_model()
    assert not hasattr(model, "models") and model.model_name == "vendor/only"


@pytest.mark.parametrize("value", [None, "", " , "])
def test_unset_or_empty_env_means_the_default_chain(monkeypatch, value):
    # GitHub Actions passes an undefined repository variable as "": that must mean "use the code's defaults"
    if value is not None:
        monkeypatch.setenv("AGENT_LLM_MODELS", value)
    assert m.agent_chain_from_env() == list(m.DEFAULT_AGENT_CHAIN)


def test_the_old_split_names_are_ignored_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_LLM_TARGET_MODEL", "old/primary")
    monkeypatch.setenv("AGENT_LLM_FALLBACK_MODEL", "old/fallback")
    with caplog.at_level(logging.WARNING):
        assert m.agent_chain_from_env() == list(m.DEFAULT_AGENT_CHAIN)
    assert "AGENT_LLM_TARGET_MODEL" in caplog.text and "AGENT_LLM_MODELS" in caplog.text


NOW = 1790164800  # 2026-09-23 12:00 UTC
CAP = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790208000000"}  # 2026-09-24 00:00 UTC, in ms
PER_MIN = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str((NOW + 40) * 1000)}
IN_FLIGHT = {"limit_source": "openrouter_in_flight_budget"}


def http(code, headers=None, body=None):
    return ModelHTTPError(code, "m", body=body, headers=headers)


@pytest.mark.parametrize("exc, expected", [
    (http(429, CAP), "stop"),                                      # account-wide free daily cap
    (http(429), "retry"),                                          # per-model upstream limit
    (http(429, {"x-ratelimit-remaining": "7"}), "retry"),
    (http(401), "stop"),
    (http(402), "stop"),
    (http(402, body={"code": 402, "message": "x", "metadata": IN_FLIGHT}), "retry"),
    (http(402, body={"error": {"code": 402, "metadata": IN_FLIGHT}}), "retry"),
    *[(http(c), "retry") for c in (408, 500, 502, 503, 504)],
    *[(http(c), "next") for c in (400, 403, 404, 409, 413, 422)],
    (ModelAPIError("m", "connection reset"), "retry"),
    (UnexpectedModelBehavior("garbled"), "next"),
    (RuntimeError("?"), "next"),
])
def test_classify_error(exc, expected):
    assert m.classify_error(exc) == expected


def test_the_daily_cap_is_told_apart_from_the_per_minute_limit():
    """Both windows answer 429 with x-ratelimit-remaining: 0. Only the daily one may stop the chain; stopping on the
    per-minute one would send every question to the canned query for the rest of the minute."""
    assert m.classify_error(http(429, PER_MIN)) == "retry"                                   # resets in 40 s
    assert m.classify_error(http(429, CAP, body={"message": "Rate limit exceeded: free-models-per-min."})) == "retry"
    assert m.classify_error(http(429, PER_MIN, body={"message": "Rate limit exceeded: free-models-per-day"})) == "stop"
    # OpenRouter may put the rate-limit headers only in the error body's metadata, in any case
    in_body = {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day",
                         "metadata": {"headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790208000000"}}}}
    assert m.classify_error(http(429, body=in_body)) == "stop"
    assert m.daily_cap(http(429, body=in_body)) == "daily free-model cap reached (resets 2026-09-24 00:00 UTC)"


def test_daily_cap_says_when_it_resets_and_survives_bad_headers():
    assert m.daily_cap(http(429)) is None and m.daily_cap(http(503, CAP)) is None
    assert m.daily_cap(http(429, CAP)) == "daily free-model cap reached (resets 2026-09-24 00:00 UTC)"
    for raw in ("soon", "9" * 30):  # an unreadable reset is only the daily cap when the message says so
        per_day = {"message": "free-models-per-day"}
        assert m.daily_cap(http(429, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": raw}, per_day)).endswith(f"(resets {raw})")
        assert m.daily_cap(http(429, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": raw})) is None


def flaky(*errors, name="m/x"):
    """A model that raises each error in turn, then answers "ok" — on both paths. Returns (model, calls)."""
    errs, calls = list(errors), []

    def fn(messages, info):
        calls.append(1)
        if errs:
            raise errs.pop(0)
        return ModelResponse(parts=[TextPart("ok")])

    async def stream_fn(messages, info):
        calls.append(1)
        if errs:
            raise errs.pop(0)
        yield "ok"

    return FunctionModel(fn, stream_function=stream_fn, model_name=name), calls


def test_retrying_retries_transient_errors_then_answers(no_waits):
    model, calls = flaky(http(503), http(503))
    assert Agent(m.Retrying(model)).run_sync("q").output == "ok"
    assert len(calls) == 3 and no_waits == [2, 5]


def test_retrying_gives_up_after_two_retries(no_waits):
    model, calls = flaky(http(503), http(503), http(503))
    with pytest.raises(ModelHTTPError):
        Agent(m.Retrying(model)).run_sync("q")
    assert len(calls) == 3


def test_retrying_never_retries_next_or_stop(no_waits):
    for err in (http(404), http(401), http(429, CAP)):
        model, calls = flaky(err)
        with pytest.raises(ModelHTTPError):
            Agent(m.Retrying(model)).run_sync("q")
        assert len(calls) == 1 and no_waits == []


def test_retry_after_is_capped_and_bad_values_use_the_default(no_waits):
    for value, expected in (("30", 10), ("3", 3), ("nan", 2), ("-5", 2), ("Wed, 24 Sep 2026 00:00:00 GMT", 2)):
        no_waits.clear()
        model, _ = flaky(http(503, {"retry-after": value}))
        Agent(m.Retrying(model)).run_sync("q")
        assert no_waits == [expected], value


def test_the_streaming_path_retries_too(no_waits):
    model, calls = flaky(http(503))

    async def handler(ctx, stream):
        async for _ in stream:
            pass

    assert Agent(m.Retrying(model)).run_sync("q", event_stream_handler=handler).output == "ok"
    assert len(calls) == 2


def chain_of(*models):
    by_name = {x.model_name: x for x in models}
    return m.build_chain(list(by_name), by_name.__getitem__)


def test_daily_cap_stops_the_whole_chain(no_waits):
    a, a_calls = flaky(http(429, CAP), name="a/x")
    b, b_calls = flaky(name="b/x")
    with pytest.raises(ModelHTTPError):
        Agent(chain_of(a, b)).run_sync("q")
    assert len(a_calls) == 1 and b_calls == [] and no_waits == []


def test_bad_key_stops_the_whole_chain(no_waits):
    a, _ = flaky(http(401), name="a/x")
    b, b_calls = flaky(name="b/x")
    with pytest.raises(ModelHTTPError):
        Agent(chain_of(a, b)).run_sync("q")
    assert b_calls == []


def test_withdrawn_model_falls_over_without_retrying(no_waits):
    a, a_calls = flaky(http(404), name="a/x")
    b, _ = flaky(name="b/x")
    assert Agent(chain_of(a, b)).run_sync("q").output == "ok"
    assert len(a_calls) == 1 and no_waits == []


def test_exhausted_retries_fall_over(no_waits):
    a, a_calls = flaky(http(503), http(503), http(503), name="a/x")
    b, _ = flaky(name="b/x")
    assert Agent(chain_of(a, b)).run_sync("q").output == "ok"
    assert len(a_calls) == 3


def test_build_chain_shapes():
    assert m.build_chain([], lambda n: None) is None
    one = m.build_chain(["a/x"], lambda n: FunctionModel(lambda *_: None, model_name=n))
    assert not hasattr(one, "models") and one.model_name == "a/x"


def test_build_model_is_the_default_chain_with_sdk_retries_off():
    chain = m.build_model(api_key="k")
    assert [x.model_name for x in chain.models] == list(m.DEFAULT_AGENT_CHAIN)
    # the SDK silently retried the daily-cap 429 for hours (evals-nightly, 2026-09-23); Retrying is the only layer
    assert all(x.client.max_retries == 0 for x in chain.models)


@pytest.mark.parametrize("value, expected", [
    (None, ["p/0", "p/1"]),
    ("", ["p/0", "p/1"]),
    ("a/x", ["a/x"]),
    ("a/x,b/y", ["a/x", "b/y"]),
    (" a/x , ,b/y,a/x\n", ["a/x", "b/y"]),   # spaces, empty items, duplicates, a pasted newline
])
def test_chain_from(value, expected):
    assert m.chain_from(value, ("p/0", "p/1")) == expected


def named(*names):
    return m.build_chain(list(names), lambda n: FunctionModel(lambda *_: None, model_name=n))


def test_after_is_the_rest_of_the_chain():
    chain = named("a/x:free", "b/y:free", "c/z:free")
    assert [x.model_name for x in m.after(chain, "a/x").models] == ["b/y:free", "c/z:free"]
    assert m.after(chain, "b/y-20260901").model_name == "c/z:free"      # providers append versions, drop :free
    assert m.after(chain, "c/z") is None                                  # the last model failed
    assert m.after(chain, "d/w") is None and m.after(chain, None) is None # not in the chain: no re-run
    assert m.after(named("a/x:free"), "a/x") is None                      # single model
    assert m.after(named("a/x", m.ROUTER), "some/routed-model") is None   # openrouter/free answers as the routed model


def test_after_prefers_the_most_specific_name():
    chain = named("a/x", "a/x-large", "c/z")
    assert m.after(chain, "a/x-large").model_name == "c/z"   # not a re-run on a/x-large itself
