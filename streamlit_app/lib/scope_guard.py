"""
lib/scope_guard.py — Off-topic question gate (runs BEFORE the LLM).

Philosophy change (v2):
  - OLD design used an ALLOWLIST (must contain marketing keywords). This
    rejected legitimate questions like "what is this dashboard about?".
  - NEW design uses a BLOCKLIST: only reject questions that are obviously
    off-topic (cooking, weather, poetry, code requests, etc.). Everything
    else is allowed through, and the downstream LLM + SQL validator handle
    the rest. This is much more user-friendly.

Real safety guarantees come from:
  - sql_validator.py (defense-in-depth on generated SQL)
  - BigQuery IAM (read-only service account)
  - Prompt-injection detection (in sql_validator.sanitize_user_input)

The scope guard's job is now narrow: catch the obvious-trash cases cheaply.
"""
from __future__ import annotations

import re

# Phrases that explicitly indicate off-topic content.
# Each pattern targets a clearly non-marketing domain.
_OFF_TOPIC_PATTERNS = re.compile(
    r"\b("
    # Cooking / recipes
    r"recipe|cook|bake|ingredient|meal|breakfast|lunch|dinner|"
    # Weather
    r"weather|temperature|forecast.+(today|tomorrow|weekend)|rain|snow|sunny|"
    # Programming / code generation requests (we're not a code generator)
    r"write.+(code|script|program|function|class|sql\b(?!.*marketing))|"
    r"how.do.i.code|write.+(python|javascript|java|c\+\+|rust|go)|"
    # Creative writing
    r"poem|haiku|sonnet|limerick|story|joke|song|lyrics|essay|"
    # Personal / off-topic
    r"horoscope|astrology|dating|relationship.advice|"
    r"medical.advice|symptom|diagnos|"
    # Translation / language
    r"translate.+(to|from).+(spanish|french|german|chinese|japanese)|"
    r"what.does.+mean.in.+(spanish|french|german|chinese)|"
    # Math homework
    r"solve.+(equation|integral|derivative|matrix)|"
    # General knowledge
    r"capital.of|population.of|who.invented|who.discovered|"
    r"history.of.(?!.*marketing)|"
    # Specific known off-topic
    r"play.+(game|chess|sudoku)|tell.+(joke|story)"
    r")\b",
    re.IGNORECASE,
)

REFUSAL_MESSAGE = (
    "I'm focused on your marketing performance data — spend, conversions, "
    "campaigns, anomalies, forecasts, and budget recommendations. "
    "That question is outside of what I can help with. "
    "Try asking something like \"How did Facebook perform last month?\" or "
    "\"What should I do about TikTok?\""
)


def is_in_scope(question: str) -> bool:
    """
    Return True for any question that isn't OBVIOUSLY off-topic.

    The blocklist catches: recipes, weather, code generation, creative writing,
    medical/legal/personal advice, translation, math homework, general trivia.

    Everything else passes through — including:
      - "What is this dashboard about?"
      - "Explain how Cost per Acquisition works"
      - "Why is Facebook performing well?"
      - "Help me understand the data"
    """
    if not question or not question.strip():
        return False
    return not bool(_OFF_TOPIC_PATTERNS.search(question))