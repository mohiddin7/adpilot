"""Model chain: OpenRouter primary → OpenRouter fallback, both behind the process rate limiter."""

from __future__ import annotations

import os

from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel

from adpilot.core.guardrails import MODEL_RATE_LIMITER

DEFAULT_PRIMARY = "inclusionai/ling-3.0-flash-vl:free"
DEFAULT_FALLBACK = "google/gemma-4-26b-a4b-it:free"


class RateLimited(WrapperModel):
    """Blocks until the 20 rpm bucket has room. Free-tier 429s are cheaper to avoid than to retry."""

    async def request(self, messages, model_settings, model_request_parameters):
        MODEL_RATE_LIMITER.acquire()  # ponytail: blocking sleep inside async; fine for CLI + single-worker API
        return await super().request(messages, model_settings, model_request_parameters)


def api_key_from_env() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LLM_BEARER_TOKEN") or None


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
    primary = primary or os.environ.get("LLM_TARGET_MODEL", DEFAULT_PRIMARY)
    fallback = fallback if fallback is not None else os.environ.get("LLM_FALLBACK_MODEL", DEFAULT_FALLBACK)
    chain = [RateLimited(OpenRouterModel(primary, provider=provider))]
    if fallback:
        chain.append(RateLimited(OpenRouterModel(fallback, provider=provider)))
    return FallbackModel(*chain) if len(chain) > 1 else chain[0]
