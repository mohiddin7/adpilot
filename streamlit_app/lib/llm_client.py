"""
lib/llm_client.py — Model-agnostic OpenAI-compatible REST client with robust
rate-limit handling and conversation memory.

Critical features:
  - 429 handling: parses retry-after from headers AND from Groq's error body
    ("Please try again in 1.065s") and waits that long before retrying.
  - Model fallback on TPM exhaustion: if `LLM_FALLBACK_MODEL` is configured,
    a 429 caused by tokens-per-minute limits triggers an immediate switch to
    the fallback model (typically one with a larger TPM allowance).
  - Conversation memory: `messages` parameter accepts arbitrary message list,
    not just one user turn. Used by the chat page to pass history.
  - 4xx (non-429): not retried — return None immediately.
  - 5xx, timeouts, connection errors: retried with exponential backoff.
  - Returns the assistant text or None on failure — callers fall back gracefully.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import requests
import streamlit as st

from . import config

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4              # transient failures: timeout/5xx/429
_MAX_SHRINK_ATTEMPTS = 6        # 413 payload-too-large: shrink-and-retry cycles
_BASE_BACKOFF = 2.0            # exponential backoff base for non-429 errors
_TIMEOUT_SEC  = 45
_MAX_RETRY_WAIT = 30.0         # cap on any single retry-after wait

# Regex that extracts the "try again in X.Xs" hint from Groq-style error bodies.
_RETRY_HINT = re.compile(r"try again in ([\d.]+)\s*s", re.IGNORECASE)

# Groq's TPM 429: "tokens per minute (TPM)"
_IS_TPM_LIMIT = re.compile(r"(tokens per minute|\bTPM\b)", re.IGNORECASE)

# Groq's TPD 429: "tokens per day (TPD)"
_IS_TPD_LIMIT = re.compile(r"(tokens per day|\bTPD\b)", re.IGNORECASE)

# Extract the human-readable wait from "Please try again in 33m54.72s" or "in 284ms"
# Captures groups: optional "Xm" prefix, then mandatory "Y.Zs"
_TPD_WAIT_HINT = re.compile(
    r"try again in\s+(?:(\d+)m)?([\d.]+)s",
    re.IGNORECASE,
)


@st.cache_resource(show_spinner=False)
def _get_session() -> requests.Session:
    """Reusable requests.Session — created once per app process."""
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "User-Agent":   "improvado-streamlit/1.1",
    })
    return s


class LLMClient:
    """OpenAI-compatible REST client. All three config values must be set."""

    def __init__(self) -> None:
        self._endpoint       = config.LLM_ENDPOINT_URL
        self._token          = config.LLM_BEARER_TOKEN
        self._primary_model  = config.LLM_TARGET_MODEL
        self._fallback_model = config.LLM_FALLBACK_MODEL or None
        # Set by complete() when a request fails due to daily token limit (TPD).
        # Cleared on every successful call. Lets callers surface a useful wait time.
        self._last_tpd_wait: Optional[str] = None

    # ── Availability ─────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """True when endpoint, token, and primary model are configured."""
        return bool(self._endpoint and self._token and self._primary_model)

    @property
    def model_name(self) -> str:
        return self._primary_model if self._primary_model else "not-configured"

    @property
    def has_fallback(self) -> bool:
        return bool(self._fallback_model)

    @property
    def daily_limit_message(self) -> Optional[str]:
        """
        Non-None when the last complete() call failed due to the DAILY token
        quota (TPD) being exhausted. Contains a user-friendly message with the
        wait duration extracted from Groq's error body.

        Cleared automatically on the next successful complete() call.

        Usage in cot_chat.py:
            result = llm.complete(...)
            if result is None and llm.daily_limit_message:
                return ChatResponse(content=llm.daily_limit_message, ...)
        """
        return self._last_tpd_wait

    # ── Core completion ──────────────────────────────────────────────────────

    def complete(
        self,
        system_prompt:       str,
        user_prompt:         Optional[str] = None,
        max_tokens:          int   = 400,
        temperature:         float = 0.2,
        messages:            Optional[list[dict]] = None,
        prefer_long_context: bool = False,
    ) -> Optional[str]:
        """
        Call the LLM and return the assistant message text, or None on failure.

        Args:
            system_prompt:       Sets the model's role and constraints.
            user_prompt:         Single user turn (convenience for one-shot calls).
            max_tokens:          Maximum completion tokens.
            temperature:         0.0–0.2 for deterministic, 0.3+ for narrative.
            messages:            Full message list (alternative to user_prompt).
                                 Use this to pass conversation history. When
                                 provided, user_prompt is ignored.
            prefer_long_context: Start with the fallback (long-context) model.

        Returns:
            String content of the first choice, or None on total failure.
        """
        if not self.is_available:
            log.warning("LLMClient.complete called but client is not available")
            return None

        # Build messages array
        if messages:
            # Always prepend the system message (overriding any caller-supplied one)
            msg_list = [{"role": "system", "content": system_prompt}]
            for m in messages:
                if m.get("role") == "system":
                    continue
                msg_list.append({"role": m["role"], "content": m["content"]})
        else:
            if user_prompt is None:
                log.error("complete() requires either user_prompt or messages")
                return None
            msg_list = [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ]

        # Choose starting model
        model = (
            self._fallback_model
            if prefer_long_context and self._fallback_model
            else self._primary_model
        )

        session = _get_session()
        headers = {"Authorization": f"Bearer {self._token}"}

        last_err: Optional[str] = None
        switched_to_fallback = False

        # Two independent counters:
        #   attempt      — transient failures (timeout/5xx/429): capped at _MAX_ATTEMPTS
        #   shrink_count — 413 payload-too-large shrinks: capped at _MAX_SHRINK_ATTEMPTS
        # A 413 shrink does NOT consume a transient-attempt — shrinking the
        # payload is a fix, not a retry-the-same-thing. The overall loop is
        # still bounded by _MAX_ATTEMPTS + _MAX_SHRINK_ATTEMPTS iterations.
        attempt = 0
        shrink_count = 0

        while True:
            total_iterations = attempt + shrink_count
            if total_iterations >= _MAX_ATTEMPTS + _MAX_SHRINK_ATTEMPTS:
                # Absolute safety cap — should never trigger given the
                # per-category caps below, but prevents any infinite loop.
                break

            payload = {
                "model":       model,
                "max_tokens":  max_tokens,
                "temperature": temperature,
                "messages":    msg_list,
            }
            try:
                resp = session.post(
                    self._endpoint,
                    json=payload,
                    headers=headers,
                    timeout=_TIMEOUT_SEC,
                )
            except requests.exceptions.Timeout as exc:
                attempt += 1
                last_err = f"timeout: {exc}"
                log.warning("LLM timeout attempt %d/%d", attempt, _MAX_ATTEMPTS)
                if attempt >= _MAX_ATTEMPTS:
                    break
                time.sleep(min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_RETRY_WAIT))
                continue
            except requests.exceptions.ConnectionError as exc:
                attempt += 1
                last_err = f"connection: {exc}"
                log.warning("LLM connection error attempt %d/%d: %s",
                            attempt, _MAX_ATTEMPTS, exc)
                if attempt >= _MAX_ATTEMPTS:
                    break
                time.sleep(min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_RETRY_WAIT))
                continue
            except requests.exceptions.RequestException as exc:
                attempt += 1
                last_err = f"request: {exc}"
                log.warning("LLM request error attempt %d/%d: %s",
                            attempt, _MAX_ATTEMPTS, exc)
                if attempt >= _MAX_ATTEMPTS:
                    break
                time.sleep(min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_RETRY_WAIT))
                continue

            # ── 429 — rate limited ───────────────────────────────────────────
            if resp.status_code == 429:
                is_tpd = _is_tpd_limit_response(resp)
                is_tpm = _is_tpm_limit_response(resp)
                wait_s = _parse_retry_after(resp)

                # ── TPD (tokens per DAY) — daily quota exhausted ──────────────
                # The wait time is typically 30-60 minutes. Retrying makes no
                # sense. Surface a user-friendly message immediately.
                if is_tpd:
                    wait_str = _parse_wait_str(resp) or "some time"
                    self._last_tpd_wait = (
                        f"The AI assistant has reached its daily usage limit. "
                        f"Please try again in {wait_str}."
                    )
                    log.error(
                        "LLM 429 TPD on model=%s — daily limit exhausted. "
                        "User must wait: %s",
                        model, wait_str,
                    )
                    return None   # caller checks llm.daily_limit_message

                # ── TPM (tokens per MINUTE) — short wait, always retry ────────
                self._last_tpd_wait = None  # not a daily limit error
                attempt += 1

                log.warning(
                    "LLM 429 TPM on model=%s attempt=%d/%d wait=%.2fs",
                    model, attempt, _MAX_ATTEMPTS, wait_s,
                )

                # Switch to fallback model immediately (different TPM bucket)
                if (is_tpm and self._fallback_model
                        and not switched_to_fallback
                        and model != self._fallback_model):
                    log.info("Switching to fallback model: %s → %s",
                             model, self._fallback_model)
                    model = self._fallback_model
                    switched_to_fallback = True
                    continue

                if attempt >= _MAX_ATTEMPTS:
                    last_err = f"429 after {_MAX_ATTEMPTS} attempts"
                    break
                time.sleep(min(wait_s, _MAX_RETRY_WAIT))
                continue

            # ── 413 — payload too large: shrink and retry ────────────────────
            if resp.status_code == 413:
                shrink_count += 1
                log.warning(
                    "LLM 413 on model=%s shrink=%d/%d msg_count=%d — "
                    "payload too large, attempting to shrink",
                    model, shrink_count, _MAX_SHRINK_ATTEMPTS, len(msg_list),
                )
                if shrink_count > _MAX_SHRINK_ATTEMPTS:
                    last_err = f"413 after {_MAX_SHRINK_ATTEMPTS} shrink attempts"
                    log.error("LLM 413 — exceeded max shrink attempts")
                    break

                shrunk, how = _shrink_messages(msg_list)
                if shrunk is None:
                    # Can't shrink further (down to just system + a tiny user msg)
                    last_err = "413 — payload could not be shrunk further"
                    log.error("LLM 413 — payload could not be shrunk further")
                    break

                log.info("LLM 413 — shrunk payload (%s); retrying", how)
                msg_list = shrunk
                continue

            # ── Other 4xx — client error, don't retry ────────────────────────
            if 400 <= resp.status_code < 500:
                err_text = resp.text[:500]
                log.error("LLM %d (no retry): %s", resp.status_code, err_text)
                return None

            # ── 5xx — transient server error, retry with backoff ─────────────
            if resp.status_code >= 500:
                attempt += 1
                last_err = f"{resp.status_code}: {resp.text[:200]}"
                log.warning("LLM %d attempt %d/%d", resp.status_code, attempt, _MAX_ATTEMPTS)
                if attempt >= _MAX_ATTEMPTS:
                    break
                time.sleep(min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_RETRY_WAIT))
                continue

            # ── Success ──────────────────────────────────────────────────────
            try:
                data = resp.json()
                text = (
                    data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        .strip()
                )
                usage = data.get("usage", {})
                log.debug(
                    "LLM ok | model=%s attempt=%d shrinks=%d in=%d out=%d",
                    model, attempt, shrink_count,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                )
                self._last_tpd_wait = None   # clear TPD flag on success
                return text if text else None
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                log.error("LLM response parse failed: %s | body=%s",
                          exc, resp.text[:300])
                return None

        log.error("LLM failed after %d attempt(s) + %d shrink(s): %s",
                  attempt, shrink_count, last_err)
        return None


# ── Helper functions for rate-limit parsing ──────────────────────────────────

def _parse_retry_after(response: requests.Response) -> float:
    """
    Extract the retry-after time in seconds. Tries (in order):
      1. Retry-After header (RFC 7231)
      2. retry-after-* headers some APIs use
      3. Error body 'Please try again in X.Xs'
      4. Default of 5 seconds
    """
    # Standard Retry-After header
    for hdr in ("retry-after", "Retry-After", "retry-after-ms"):
        val = response.headers.get(hdr)
        if val:
            try:
                f = float(val)
                # 'retry-after-ms' is in milliseconds
                if hdr.endswith("-ms"):
                    return max(0.0, f / 1000.0)
                return max(0.0, f)
            except ValueError:
                # RFC 7231 allows HTTP-date but we don't bother parsing it here
                pass

    # Parse Groq's "try again in 1.065s" message
    try:
        body = response.json()
        msg = body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
        m = _RETRY_HINT.search(msg)
        if m:
            return max(0.0, float(m.group(1)))
    except (json.JSONDecodeError, ValueError):
        pass

    return 5.0  # conservative default


def _is_tpm_limit_response(response: requests.Response) -> bool:
    """True when a 429 was caused by tokens-per-MINUTE exhaustion."""
    try:
        body = response.json()
        msg = body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
        return bool(_IS_TPM_LIMIT.search(msg))
    except (json.JSONDecodeError, ValueError):
        return False


def _is_tpd_limit_response(response: requests.Response) -> bool:
    """True when a 429 was caused by tokens-per-DAY exhaustion.

    Groq's TPD error message contains 'tokens per day' or 'TPD' and typically
    shows wait times in minutes (e.g., 'Please try again in 33m54.72s'),
    whereas TPM errors show millisecond waits ('Please try again in 284ms').
    """
    try:
        body = response.json()
        msg = body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
        if bool(_IS_TPD_LIMIT.search(msg)):
            return True
        # Heuristic fallback: if neither TPM nor TPD keyword found but the
        # wait time is > 60 seconds, it's almost certainly daily/hourly quota.
        wait_s = _parse_retry_after(response)
        return wait_s > 60.0 and not bool(_IS_TPM_LIMIT.search(msg))
    except (json.JSONDecodeError, ValueError):
        return False


def _parse_wait_str(response: requests.Response) -> Optional[str]:
    """
    Extract the human-readable wait time string from a Groq 429 body.

    Handles both formats:
      "Please try again in 33m54.72s"  → "33m 54s"
      "Please try again in 284ms"       → "284ms"  (won't be TPD, but safe)
      "Please try again in 3.5s"        → "3.5s"
    """
    try:
        body = response.json()
        msg = body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
        m = _TPD_WAIT_HINT.search(msg)
        if m:
            mins_str, secs_str = m.group(1), m.group(2)
            if mins_str:
                mins = int(mins_str)
                secs = int(float(secs_str))
                return f"{mins}m {secs}s"
            return f"{float(secs_str):.0f}s"
        # Second pass: raw "in Xms" (millisecond format)
        ms_m = re.search(r"try again in\s+([\d]+)ms", msg, re.IGNORECASE)
        if ms_m:
            return f"{ms_m.group(1)}ms"
    except Exception:
        pass
    return None


# ── 413 payload-shrinking ──────────────────────────────────────────────────────
#
# Strategy, in order (each step tried once per 413 response):
#   1. Drop the OLDEST history pair (user+assistant right after system).
#      Repeat until only [system, latest_user] remains.
#   2. Once down to [system, latest_user], truncate the user message content
#      to progressively smaller fractions (75% → 50% → 25%).
#   3. If the user message is already tiny and step 2 can't shrink further,
#      give up — return None so the caller stops retrying.
#
# This mirrors the self-healing SQL pattern: don't just fail, adapt the
# request based on the specific error, then retry.

_MIN_USER_MSG_CHARS = 200          # below this, truncation stops helping
_TRUNCATION_SUFFIX  = "\n\n[...truncated to fit request size limit...]"


def _shrink_messages(msg_list: list[dict]) -> tuple[Optional[list[dict]], str]:
    """
    Return (shrunk_messages, description) or (None, "") if no further
    shrinking is possible.

    msg_list[0] is always the system message (added by complete()).
    msg_list[1:] is history + the latest user message (always last).
    """
    if len(msg_list) > 2:
        # Step 1: drop the oldest non-system message (msg_list[1])
        dropped = msg_list[1]
        new_list = [msg_list[0]] + msg_list[2:]
        return new_list, f"dropped oldest history message (role={dropped.get('role')})"

    # Down to [system, latest_user] (or just [system] in a degenerate case)
    if len(msg_list) < 2:
        return None, ""

    last = msg_list[-1]
    content = last.get("content", "") or ""

    if len(content) <= _MIN_USER_MSG_CHARS:
        # Already small — nothing more to shrink
        return None, ""

    # Step 2: truncate to ~60% of current length (leaves room across retries:
    # e.g. 2000 → 1200 → 720 → 432 → stops once <= _MIN_USER_MSG_CHARS)
    new_len = max(_MIN_USER_MSG_CHARS, int(len(content) * 0.6))
    truncated = content[:new_len] + _TRUNCATION_SUFFIX

    new_last = dict(last)
    new_last["content"] = truncated
    new_list = msg_list[:-1] + [new_last]
    return new_list, f"truncated last message {len(content)}→{len(truncated)} chars"