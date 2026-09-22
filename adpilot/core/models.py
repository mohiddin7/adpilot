"""Model chain: OpenRouter primary → OpenRouter fallback, both behind the process rate limiter."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel

from adpilot.core.guardrails import MODEL_RATE_LIMITER

log = logging.getLogger(__name__)

# Measured on the calibration set and a full model-tier A/B, 2026-09-21. gemma-4's free tier shares a
# Google AI Studio quota that was exhausted for a whole run, so it is no longer in either chain.
DEFAULT_PRIMARY = "inclusionai/ling-3.0-flash-vl:free"
DEFAULT_FALLBACK = "poolside/laguna-xs-2.1:free"


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
    """Blocks until the 20 rpm bucket has room. Free-tier 429s are cheaper to avoid than to retry."""

    async def request(self, messages, model_settings, model_request_parameters):
        MODEL_RATE_LIMITER.acquire()  # ponytail: blocking sleep inside async; fine for CLI + single-worker API
        return await super().request(messages, model_settings, model_request_parameters)


def api_key_from_env() -> str | None:
    return renamed_env("AGENT_LLM_BEARER_TOKEN", "OPENROUTER_API_KEY", "LLM_BEARER_TOKEN") or None


def agent_primary_from_env() -> str:
    return renamed_env("AGENT_LLM_TARGET_MODEL", "LLM_TARGET_MODEL", default=DEFAULT_PRIMARY) or DEFAULT_PRIMARY


def agent_fallback_from_env() -> str | None:
    """An explicit empty value means "no fallback"; an unset variable means the default chain."""
    return renamed_env("AGENT_LLM_FALLBACK_MODEL", "LLM_FALLBACK_MODEL", default=DEFAULT_FALLBACK)


def build_model(
    primary: str | None = None,
    fallback: str | None = None,
    api_key: str | None = None,
) -> Model | None:
    """Return the configured model chain, or None when no API key is available (rule-based mode)."""
    api_key = api_key or api_key_from_env()
    if not api_key:
        return None
    from pydantic_ai.models.openrouter import OpenRouterModel
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    provider = OpenRouterProvider(api_key=api_key)
    primary = primary or agent_primary_from_env()
    fallback = fallback if fallback is not None else agent_fallback_from_env()
    chain = [RateLimited(OpenRouterModel(primary, provider=provider))]
    if fallback:
        chain.append(RateLimited(OpenRouterModel(fallback, provider=provider)))
    return FallbackModel(*chain) if len(chain) > 1 else chain[0]
