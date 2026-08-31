"""Model chains: free models in order, each rate-limited and retried under one error policy (docs/models.md)."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel

from adpilot.core.guardrails import MODEL_RATE_LIMITER

log = logging.getLogger(__name__)

# Live probe of every tool-capable free model, 2026-09-23 (docs/models.md). openrouter/free is last in every chain:
# it routes to whichever free model is up.
ROUTER = "openrouter/free"
DEFAULT_AGENT_CHAIN = (
    "inclusionai/ling-3.0-flash-fin:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "nvidia/nemotron-3.5-lightning:free",
    ROUTER,
)
MAX_RETRIES = 2
RETRY_DELAYS = (2, 5)
MAX_RETRY_DELAY = 10
DAILY_RESET_MIN_S = 120  # a per-minute window resets within 60 s

Disposition = Literal["retry", "next", "stop"]
_TRANSIENT = {408, 429, 500, 502, 503, 504}
_sleep = time.sleep  # tests patch these two
_now = time.time


def renamed_env(name: str, *legacy: str, env: Mapping[str, str] | None = None, default: str | None = None) -> str | None:
    """Read `name`. Legacy spellings are ignored, never aliased — but say so, loudly enough to find.

    A silently ignored key reads as "no key configured" three layers away; a stale duplicate beside a
    live one is how local, CI and the dashboard drifted apart in the first place.
    """
    env = os.environ if env is None else env
    stale = [o for o in legacy if env.get(o)]
    if stale:
        if name in env:
            log.warning("%s is set and ignored (renamed to %s, which is also set) — delete the old name", ", ".join(stale), name)
        else:
            log.warning("%s is set but ignored: this variable was renamed to %s", ", ".join(stale), name)
    return env[name] if name in env else default


class RateLimited(WrapperModel):
    """Blocks until the 20 rpm bucket has room, on both the request and the streaming path.

    Free-tier 429s are cheaper to avoid than to retry.
    """

    async def request(self, messages, model_settings, model_request_parameters):
        MODEL_RATE_LIMITER.acquire()  # ponytail: blocking sleep inside async; fine for CLI + single-worker API
        return await super().request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        # WrapperModel.request_stream delegates straight through, so without this override the streaming path
        # (ask() with an event_stream_handler, i.e. GET /ask/stream) would bypass the bucket entirely.
        MODEL_RATE_LIMITER.acquire()
        async with super().request_stream(messages, model_settings, model_request_parameters, run_context) as stream:
            yield stream


def daily_cap(exc: BaseException) -> str | None:
    """A caveat for OpenRouter's account-wide free daily cap, else None.

    The per-minute window also answers 429 with `x-ratelimit-remaining: 0`, so remaining 0 alone is not enough: the
    message must name the per-day limit, or (naming neither) the reset must be minutes away."""
    if not (isinstance(exc, ModelHTTPError) and exc.status_code == 429):
        return None
    headers = _rate_headers(exc)
    if headers.get("x-ratelimit-remaining") != "0":
        return None
    text, raw = str(exc.body), headers.get("x-ratelimit-reset", "unknown")
    try:
        reset_s: float | None = int(raw) / 1000
        when = f"{datetime.fromtimestamp(reset_s, UTC):%Y-%m-%d %H:%M} UTC"
    except (ValueError, OverflowError, OSError):
        reset_s, when = None, raw
    daily = "per-day" in text or ("per-min" not in text and reset_s is not None and reset_s - _now() > DAILY_RESET_MIN_S)
    return f"daily free-model cap reached (resets {when})" if daily else None


def _rate_headers(exc: ModelHTTPError) -> dict[str, str]:
    """The response headers, over the copy OpenRouter puts in the error body's metadata; keys lower-cased."""
    in_body = _metadata(exc).get("headers")
    headers = {str(k).lower(): str(v) for k, v in in_body.items()} if isinstance(in_body, dict) else {}
    return {**headers, **(exc.headers or {})}


def _metadata(exc: ModelHTTPError) -> dict:
    body = exc.body if isinstance(exc.body, dict) else {}
    if isinstance(body.get("error"), dict):
        body = body["error"]
    meta = body.get("metadata")
    return meta if isinstance(meta, dict) else {}


def classify_error(exc: BaseException) -> Disposition:
    """The one place provider error codes are read.

    retry: a retry can fix it (timeout, overload, per-model limit, network). next: this model cannot serve this
    request (schema rejected, withdrawn, harness-only). stop: no model can — the free daily cap and the key are
    per account, so falling over only burns time."""
    if isinstance(exc, ModelHTTPError):
        code = exc.status_code
        if code == 401 or daily_cap(exc):
            return "stop"
        if code == 402:
            return "retry" if _metadata(exc).get("limit_source") == "openrouter_in_flight_budget" else "stop"
        return "retry" if code in _TRANSIENT else "next"
    if isinstance(exc, ModelAPIError):  # no status: connection reset, DNS, read timeout
        return "retry"
    return "next"  # malformed response, anything unknown: fail over, never hang


def _retry_delay(exc: BaseException, attempt: int) -> float:
    try:
        wait = float((getattr(exc, "headers", None) or {})["retry-after"])
    except (KeyError, ValueError):
        wait = -1
    return min(wait, MAX_RETRY_DELAY) if wait >= 0 else RETRY_DELAYS[attempt]  # nan and negatives fail `>= 0`


async def _with_retries(call):
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await call()
        except Exception as exc:
            if attempt == MAX_RETRIES or classify_error(exc) != "retry":
                raise
            wait = _retry_delay(exc, attempt)
            log.info("model call failed (%s), retry %d in %.0f s", exc.__class__.__name__, attempt + 1, wait)
            _sleep(wait)  # ponytail: blocking sleep inside async, like RateLimited; async sleep once the API runs >1 worker


class Retrying(WrapperModel):
    """Retries what classify_error calls transient, at most MAX_RETRIES times; re-raises everything else at once."""

    async def request(self, messages, model_settings, model_request_parameters):
        return await _with_retries(lambda: self.wrapped.request(messages, model_settings, model_request_parameters))

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        # Only opening the stream is retried: once events reach the caller, a retry would replay them.
        async with AsyncExitStack() as stack:
            yield await _with_retries(lambda: stack.enter_async_context(
                self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context)
            ))


def _falls_over(exc):  # untyped on purpose: FallbackModel reads a typed ModelResponse parameter as a response handler
    return classify_error(exc) != "stop"


def _chain(models: Sequence[Model]) -> Model | None:
    if not models:
        return None
    return models[0] if len(models) == 1 else FallbackModel(*models, fallback_on=_falls_over)


def build_chain(names: Sequence[str], make: Callable[[str], Model]) -> Model | None:
    """names[0] → names[1] → …; every attempt passes the 20 rpm bucket; a stop error ends the chain at once."""
    return _chain([Retrying(RateLimited(make(n))) for n in names])


def openrouter_factory(api_key: str) -> Callable[[str], Model]:
    from pydantic_ai.models.openrouter import OpenRouterModel
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    provider = OpenRouterProvider(api_key=api_key)
    # Retrying is the only retry layer: the SDK's own retried the daily-cap 429 for hours (evals-nightly, 2026-09-23).
    provider.client.max_retries = 0
    return lambda name: OpenRouterModel(name, provider=provider)


def chain_names(primary: str | None, fallback: str | None, default: Sequence[str]) -> list[str]:
    """primary (or default[0]), then fallback: None → default[1:], "" → none, "a, b" → both. Duplicates dropped, order kept."""
    rest = default[1:] if fallback is None else [n.strip() for n in fallback.split(",")]
    return list(dict.fromkeys(n for n in (primary or default[0], *rest) if n))


def api_key_from_env() -> str | None:
    return renamed_env("AGENT_LLM_BEARER_TOKEN", "OPENROUTER_API_KEY", "LLM_BEARER_TOKEN") or None


def agent_primary_from_env(env: Mapping[str, str] | None = None) -> str | None:
    return renamed_env("AGENT_LLM_TARGET_MODEL", "LLM_TARGET_MODEL", env=env) or None


def agent_fallback_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """Unset → the default fallbacks; an explicit empty value → none; "a,b" → both, in order."""
    return renamed_env("AGENT_LLM_FALLBACK_MODEL", "LLM_FALLBACK_MODEL", env=env)


def agent_chain_from_env(env: Mapping[str, str] | None = None) -> list[str]:
    return chain_names(agent_primary_from_env(env), agent_fallback_from_env(env), DEFAULT_AGENT_CHAIN)


def build_model(primary: str | None = None, fallback: str | None = None, api_key: str | None = None) -> Model | None:
    """Return the configured model chain, or None when no API key is available (rule-based mode)."""
    api_key = api_key or api_key_from_env()
    if not api_key:
        return None
    names = chain_names(
        primary or agent_primary_from_env(),
        fallback if fallback is not None else agent_fallback_from_env(),
        DEFAULT_AGENT_CHAIN,
    )
    return build_chain(names, openrouter_factory(api_key))


def _no_null(schema: Any) -> Any:
    """anyOf [X, {"type": "null"}] → X (a default of null goes too), recursively, $defs included."""
    if isinstance(schema, list):
        return [_no_null(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _no_null(v) for k, v in schema.items()}
    alts = out.get("anyOf")
    rest = [a for a in alts if a != {"type": "null"}] if isinstance(alts, list) else []
    if rest and len(rest) < len(alts):
        del out["anyOf"]
        if "default" in out and out["default"] is None:
            del out["default"]
        out = {**(rest[0] if len(rest) == 1 else {"anyOf": rest}), **out}
    return out


class NoNullSchemas(AbstractCapability[Any]):
    """Strict-grammar providers reject a whole request over one nullable parameter. Only what the model is shown
    changes: signatures, validation and the API's wire shape still accept and return null."""

    async def prepare_tools(self, ctx, tool_defs):
        return [replace(d, parameters_json_schema=_no_null(d.parameters_json_schema)) for d in tool_defs]

    prepare_output_tools = prepare_tools


def same_model(configured: str, answered: str | None) -> bool:
    """Providers answer as the configured name without OpenRouter's `:free`, often with a date or version appended."""
    return bool(answered) and answered.startswith(configured.removesuffix(":free"))


def after(chain: Model | str | None, failed: str | None) -> Model | None:
    """The chain's models after the one that answered as `failed`; None when it was the last or is not in the chain.

    openrouter/free answers as the model it routed to, which is never earlier in the chain, so it gets None."""
    models = list(getattr(chain, "models", None) or [])
    hits = [i for i, x in enumerate(models) if same_model(x.model_name, failed)]
    if not hits:
        return None
    i = max(hits, key=lambda i: len(models[i].model_name))  # "a/x-large" answered: not a/x
    return _chain(models[i + 1:])
