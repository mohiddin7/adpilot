"""
lib/cot_chat.py — Chain-of-thought processor for the chatbot with conversation
memory.

Pipeline:

  User question + conversation history
       │
       ▼
  ┌─────────────────────────────────┐
  │ Step 1: Intent classifier (LLM) │
  │   → factual | suggestion |       │
  │     analytical | off_topic       │
  │   (history-aware — handles       │
  │    follow-ups like "now TikTok") │
  └────────┬───────────────┬─────────┘
           │               │
       factual         suggestion / analytical
           │               │
           ▼               ▼
   ┌──────────────┐  ┌─────────────────────────────┐
   │ SQL agent    │  │ Data planner (LLM)          │
   │ + history    │  │   → SQL gathering context   │
   │ → SQL        │  │ + history                   │
   │ → validate   │  └────────────┬────────────────┘
   │ → execute    │               ▼
   │ → narrate    │  ┌─────────────────────────────┐
   │ + history    │  │ Suggestion synthesizer (LLM)│
   └──────────────┘  │ + history                   │
                     │ → numbered recommendations  │
                     └─────────────────────────────┘

Every LLM call now receives the last N turns as conversation context, so:
  - "Now show TikTok instead" works as a follow-up
  - "Why is that?" references the previous answer
  - The narrator avoids repeating what was just said
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from . import config, prompts
from .bq_client import run_query_uncached, BQUserFriendlyError, discover_schemas_from_sql
from .chart_builder import (
    ChartSpec,
    ChartSpecError,
    heuristic_chart_spec,
    parse_chart_spec,
)
from .llm_client import LLMClient
from .sql_agent import SQLAgent
from .sql_validator import (
    SQLValidationError,
    sanitize_user_input,
    validate,
)
from .scope_guard import REFUSAL_MESSAGE, is_in_scope

log = logging.getLogger(__name__)

# Maximum turns of history we send to the LLM. Older turns are dropped.
# 3 turns × ~150 tokens each ≈ 450 tokens of context — well under any limit.
MAX_HISTORY_TURNS = 3

# SQL repair: how many correction attempts when BQ returns a recoverable
# error (syntax, unrecognized column, GROUP BY violations, etc.). Each
# attempt adds one LLM call + an INFORMATION_SCHEMA schema-discovery query
# (so the LLM gets REAL column names on repair, not guesses).
# 4 attempts: original execution + up to 4 schema-informed repair calls = 5 BQ ops max.
SQL_REPAIR_ATTEMPTS = 4

# Sentinel the LLM returns when a question can't be answered via SQL on our
# four tables (e.g. "give me a chart", "what's the weather", pure opinions).
_OUT_OF_SCOPE_SENTINEL = "OUT_OF_SCOPE"


def _is_out_of_scope_sql(raw_sql: str) -> bool:
    """
    True when the LLM returned the OUT_OF_SCOPE sentinel instead of real SQL.

    IMPORTANT: this check must run BEFORE validate(). The sentinel
    (`SELECT 'OUT_OF_SCOPE' AS reason LIMIT 1`) has zero backtick-quoted
    table references by design, so the validator's allowlist check would
    reject it with a confusing "No fully-qualified table reference found"
    error — even though the LLM did exactly the right thing by signalling
    "I can't answer this with SQL." Checking here lets us route to a
    friendly, helpful response instead.
    """
    if not raw_sql:
        return False
    return _OUT_OF_SCOPE_SENTINEL in raw_sql.upper()


def _out_of_scope_response(
    question: str,
    intent:   str,
    steps:    list[ReasoningStep],
    t0:       float,
) -> "ChatResponse":
    """
    Build a helpful response for questions that can't be answered via SQL.

    Detects "show me a chart / visualize that" phrasing specifically, since
    that's the most common reason the LLM returns OUT_OF_SCOPE, and points
    the user at the chart feature instead of a dead end.
    """
    steps.append(ReasoningStep(
        label="Check scope",
        detail="This can't be answered with a data query on the available tables.",
    ))

    q_lower = question.lower()
    wants_chart = any(w in q_lower for w in (
        "chart", "graph", "plot", "visuali", "visualis", "visualiz", "diagram"
    ))

    if wants_chart:
        content = (
            "I can't generate SQL for \"give me a chart\" — charts aren't a "
            "data query. But if your previous question returned a data table, "
            "ask me to \"chart that\" or \"plot it as a bar chart\" and I'll "
            "visualize the most recent result."
        )
    else:
        content = (
            "That question can't be answered from the available marketing "
            "data (spend, conversions, campaigns, anomalies, forecasts, "
            "budget recommendations). Try rephrasing around one of those topics."
        )

    return ChatResponse(content=content, intent=intent, steps=steps, duration_ms=_ms(t0))


def _execute_with_self_heal(
    sql:              str,
    original_question: str,
    llm:              LLMClient,
    system_prompt:    str,
    max_bytes:        int,
    history:          list[dict],
    steps:            list,
) -> tuple[pd.DataFrame, str, Optional[BQUserFriendlyError]]:
    """
    Execute SQL; on a recoverable error, ask the LLM to fix it and retry.

    Returns:
        (df, final_sql, error)
        - df is empty DataFrame if all attempts failed
        - final_sql is the SQL that was last attempted (for transparency)
        - error is None on success, BQUserFriendlyError otherwise

    Recoverable errors (worth retrying with feedback):
      - Syntax errors (400 invalidQuery)
      - Unrecognized column / table
      - Type mismatch

    NOT retried (would burn LLM calls without helping):
      - bytes-billed cap (need user to narrow question, not LLM to fix SQL)
      - access denied
      - timeout
    """
    current_sql = sql
    attempt = 0   # counts REPAIR calls only (original execution is attempt 0)

    while True:
        try:
            df = run_query_uncached(current_sql, max_bytes=max_bytes)
            return df, current_sql, None

        except BQUserFriendlyError as exc:
            # Classify: is this error caused by the SQL itself (fixable) or the
            # environment (bytes-billed cap, permissions, timeout — not fixable
            # by changing the SQL)?
            tech_lower = exc.technical.lower()
            is_recoverable = (
                "syntax" in tech_lower or
                "unrecognized name" in tech_lower or
                "not found in" in tech_lower or
                "type mismatch" in tech_lower or
                "invalidquery" in tech_lower or
                "neither grouped nor aggregated" in tech_lower or
                "must appear in the group by" in tech_lower
            )

            if not is_recoverable or attempt >= SQL_REPAIR_ATTEMPTS:
                log.info("SQL self-heal stopping (recoverable=%s, attempt=%d/%d)",
                         is_recoverable, attempt, SQL_REPAIR_ATTEMPTS)
                return pd.DataFrame(), current_sql, exc

            attempt += 1
            log.info("SQL repair attempt %d/%d | error: %s",
                     attempt, SQL_REPAIR_ATTEMPTS, exc.technical[:200])

            steps.append(ReasoningStep(
                label=f"Repair SQL (attempt {attempt}/{SQL_REPAIR_ATTEMPTS})",
                detail="BigQuery rejected the query — discovering actual schema and asking the model to fix it.",
                status="pending",
            ))

            # ── Schema discovery (THE KEY INNOVATION) ────────────────────
            # When BigQuery says "Unrecognized name: X", the LLM generated a
            # column that doesn't exist. Rather than just telling the LLM
            # "fix it" (which leads to more guessing), we QUERY THE ACTUAL
            # INFORMATION_SCHEMA to get the real column names and types, then
            # inject them into the repair prompt. The LLM now has ground truth.
            schema_context = ""
            if "unrecognized name" in tech_lower or "not found in" in tech_lower:
                discovered = discover_schemas_from_sql(current_sql)
                if discovered:
                    schema_context = (
                        f"\n\n{discovered}\n\n"
                        f"Use ONLY the column names shown above — do NOT invent columns."
                    )
                    steps[-1].detail += (
                        " Schema discovered from INFORMATION_SCHEMA — repair will use real column names."
                    )
            # ────────────────────────────────────────────────────────────────

            # Extract the error cleanly (strip Location: US / Job ID: lines)
            error_snippet = exc.technical
            for marker in ("\n\nLocation:", "\nLocation:", "; reason:"):
                if marker in error_snippet:
                    error_snippet = error_snippet.split(marker)[0]
                    break
            error_snippet = error_snippet.strip()[:400]

            repair_messages: list[dict] = list(history)
            repair_messages.append({
                "role": "user",
                "content": (
                    f"Original question: {original_question}\n\n"
                    f"You generated this SQL:\n```sql\n{current_sql}\n```\n\n"
                    f"BigQuery rejected it with:\n```\n{error_snippet}\n```"
                    f"{schema_context}\n\n"
                    f"Generate a CORRECTED SQL that fixes the specific error. "
                    f"Return ONLY the SQL, no explanation."
                ),
            })

            try:
                fixed_sql = llm.complete(
                    system_prompt=system_prompt,
                    messages=repair_messages,
                    max_tokens=600,
                    temperature=0.0,   # deterministic — this is a fix, not a creative task
                )
            except Exception as repair_exc:
                log.warning("SQL repair LLM call failed: %s", repair_exc)
                return pd.DataFrame(), current_sql, exc

            if not fixed_sql or not fixed_sql.strip():
                return pd.DataFrame(), current_sql, exc

            # Validate the repaired SQL through the same 9-layer safety checks
            try:
                # Skip OUT_OF_SCOPE check here — if LLM returns it on repair,
                # just let validate() pass it and the outer caller will handle it
                current_sql = validate(fixed_sql)
            except SQLValidationError as val_exc:
                steps.append(ReasoningStep(
                    label="Validate repaired SQL",
                    detail=f"Repaired SQL failed validation: {val_exc}",
                    status="error",
                ))
                return pd.DataFrame(), fixed_sql, exc

            steps[-1].status = "ok"
            steps[-1].detail = (
                f"Model returned corrected SQL "
                f"{'(with real column names from schema discovery)' if schema_context else ''}. "
                f"Retrying execution."
            )
            # Loop continues: execute current_sql again

    # Safety net — should not reach here given the attempt cap above
    return pd.DataFrame(), current_sql, BQUserFriendlyError(
        friendly="The query couldn't be executed after retries.",
        technical="exhausted retries",
    )


@dataclass
class ReasoningStep:
    """One step in the chain-of-thought trace, shown to the user."""
    label:   str
    detail:  str
    status:  str = "ok"          # ok | pending | error


@dataclass
class ChatResponse:
    """Structured response from the COT pipeline."""
    content:       str
    intent:        Optional[str] = None
    sql:           Optional[str] = None
    data:          Optional[pd.DataFrame] = None
    steps:         list[ReasoningStep] = field(default_factory=list)
    duration_ms:   int = 0
    error:         Optional[str] = None
    chart_spec:    Optional["ChartSpec"] = None   # set when intent == "visualize"


# ── History helpers ──────────────────────────────────────────────────────────

def _trim_history(history: Optional[list]) -> list[dict]:
    """
    Keep only the last MAX_HISTORY_TURNS user+assistant pairs.
    Each item should be a dict with keys: role, content.
    """
    if not history:
        return []
    # Keep last N pairs = last 2*N messages
    keep = MAX_HISTORY_TURNS * 2
    trimmed = []
    for m in history[-keep:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            # Truncate any single message to ~600 chars to keep tokens bounded
            trimmed.append({"role": role, "content": str(content)[:600]})
    return trimmed


def _history_summary_text(history: list[dict]) -> str:
    """Render history as a short text block for prompt embedding."""
    if not history:
        return ""
    lines = ["PREVIOUS CONVERSATION:"]
    for m in history:
        prefix = "User" if m["role"] == "user" else "Assistant"
        lines.append(f"  {prefix}: {m['content']}")
    return "\n".join(lines) + "\n\n"


# ── Step 1: Intent classification (history-aware) ────────────────────────────

_VALID_INTENTS = {"factual", "suggestion", "analytical", "visualize", "off_topic"}

# Keywords that strongly indicate a visualize request — checked BEFORE the
# LLM classifier so "chart that" works even when the LLM is offline.
_VISUALIZE_KEYWORDS = (
    "chart", "graph", "plot", "visuali", "visualis", "visualiz", "diagram",
)


def classify_intent(
    question: str,
    llm:      LLMClient,
    history:  Optional[list[dict]] = None,
) -> str:
    """Classify the question into one of five intents using conversation context."""
    q_lower = question.lower()

    # Visualize requests are detected by keyword regardless of LLM availability —
    # this is cheap, reliable, and the chart builder validates everything anyway.
    if any(w in q_lower for w in _VISUALIZE_KEYWORDS):
        return "visualize"

    if not llm.is_available:
        # Heuristic fallback when LLM is offline
        if any(w in q_lower for w in ("suggest", "should we", "should i", "how can",
                                       "how do we", "what should", "recommend",
                                       "improve", "optimize", "improvement")):
            return "suggestion"
        if any(w in q_lower for w in ("why", "compare", "analyze", "what's driving",
                                       "trend", "pattern", "explain")):
            return "analytical"
        return "factual"

    # Build a messages array so the classifier sees the conversation
    msgs: list[dict] = []
    if history:
        for m in history:
            msgs.append(m)
    msgs.append({"role": "user", "content": question})

    response = llm.complete(
        system_prompt=prompts.INTENT_CLASSIFIER_SYSTEM,
        messages=msgs,
        max_tokens=10,
        temperature=0.0,
    )
    if not response:
        return "factual"

    intent = response.strip().lower().split()[0] if response.strip() else "factual"
    if intent not in _VALID_INTENTS:
        return "factual"
    return intent


# ── Main entry point ──────────────────────────────────────────────────────────

def process_question(
    question:      str,
    llm:           LLMClient,
    history:       Optional[list[dict]] = None,
    previous_sql:  Optional[str] = None,
    previous_data: Optional[pd.DataFrame] = None,
) -> ChatResponse:
    """
    Process a question through the COT pipeline with conversation memory.

    Args:
        question:      The user's new question.
        llm:           LLMClient instance.
        history:       Previous chat messages [{role, content}, ...]. Trimmed to
                       MAX_HISTORY_TURNS pairs internally.
        previous_sql:  SQL from the most recent answer (for direct SQL follow-ups).
        previous_data: DataFrame from the most recent answer. Used when intent
                       is "visualize" — charts are built from this data, not a
                       new query. If None and the user asks to visualize,
                       a helpful message explains there's nothing to chart yet.

    Returns:
        ChatResponse with content, reasoning steps, and optional data/SQL/chart_spec.
    """
    t0 = time.perf_counter()
    steps: list[ReasoningStep] = []
    trimmed_history = _trim_history(history)

    # Sanitize user input (length, control chars, prompt injection)
    try:
        question_clean = sanitize_user_input(question)
    except SQLValidationError as exc:
        return ChatResponse(
            content=f"⚠️ {exc}", intent="refused",
            duration_ms=_ms(t0), error=str(exc),
        )

    # Cheap scope guard
    if not is_in_scope(question_clean):
        return ChatResponse(
            content=REFUSAL_MESSAGE, intent="off_topic",
            duration_ms=_ms(t0),
        )

    # Step 1: Classify intent (with history context for follow-ups)
    intent = classify_intent(question_clean, llm, history=trimmed_history)
    history_note = f" (with {len(trimmed_history)} prior messages)" if trimmed_history else ""
    steps.append(ReasoningStep(
        label="Classify intent",
        detail=f"Question type: **{intent}**{history_note}",
    ))

    if intent == "off_topic":
        return ChatResponse(
            content=REFUSAL_MESSAGE, intent="off_topic",
            steps=steps, duration_ms=_ms(t0),
        )

    # Route based on intent
    if intent == "factual":
        return _process_factual(question_clean, llm, trimmed_history,
                                previous_sql, steps, t0)
    elif intent == "visualize":
        return _process_visualize(question_clean, llm, previous_data, steps, t0)
    else:
        return _process_chain_of_thought(question_clean, llm, trimmed_history,
                                          intent, steps, t0)


# ── Visualize path (constrained chart spec — no code execution) ─────────────

def _process_visualize(
    question:      str,
    llm:           LLMClient,
    previous_data: Optional[pd.DataFrame],
    steps:         list[ReasoningStep],
    t0:            float,
) -> ChatResponse:
    """
    Build a chart from the PREVIOUS query's result data.

    SECURITY: the LLM only ever picks chart_type + column names from a fixed
    enum / the real DataFrame columns (see lib/chart_builder.py). No code is
    generated or executed.
    """
    if previous_data is None or previous_data.empty:
        return ChatResponse(
            content=(
                "I don't have a previous result to chart yet. Ask a data "
                "question first (e.g. \"What was spend by platform?\"), "
                "then ask me to chart it."
            ),
            intent="visualize", steps=steps, duration_ms=_ms(t0),
        )

    steps.append(ReasoningStep(
        label="Select chart parameters",
        detail=f"Choosing chart type and columns from {len(previous_data.columns)} "
               f"available columns.",
    ))

    spec: Optional[ChartSpec] = None
    spec_source = "heuristic"

    if llm.is_available:
        columns_desc = ", ".join(
            f"{c} ({previous_data[c].dtype})" for c in previous_data.columns
        )
        raw_spec = llm.complete(
            system_prompt=prompts.CHART_SPEC_SYSTEM,
            user_prompt=(
                f"User request: {question}\n\n"
                f"Table columns and types: {columns_desc}\n\n"
                f"Sample rows:\n{previous_data.head(3).to_markdown(index=False)}"
            ),
            max_tokens=150,
            temperature=0.0,
        )
        if raw_spec:
            try:
                spec = parse_chart_spec(raw_spec, previous_data)
                spec_source = "model"
            except ChartSpecError as exc:
                steps.append(ReasoningStep(
                    label="Validate chart spec",
                    detail=f"Model's chart spec was invalid ({exc}); falling back "
                           f"to a heuristic choice.",
                    status="pending",
                ))

    if spec is None:
        spec = heuristic_chart_spec(previous_data)
        spec_source = "heuristic"

    if spec is None:
        return ChatResponse(
            content=(
                "I couldn't find a natural way to chart that result — it may "
                "not have a clear category/value pair. You can view the data "
                "in the table below."
            ),
            intent="visualize", steps=steps, data=previous_data, duration_ms=_ms(t0),
        )

    steps.append(ReasoningStep(
        label="Build chart",
        detail=f"{spec.chart_type.title()} chart: {spec.y} by {spec.x}"
               f"{' (colored by ' + spec.color + ')' if spec.color else ''} "
               f"— chosen by {spec_source}.",
    ))

    return ChatResponse(
        content=spec.title or f"Here's a {spec.chart_type} chart of {spec.y} by {spec.x}.",
        intent="visualize", steps=steps, data=previous_data,
        chart_spec=spec, duration_ms=_ms(t0),
    )


# ── Factual path (one SQL, one narrative) ─────────────────────────────────────

def _process_factual(
    question:        str,
    llm:             LLMClient,
    history:         list[dict],
    previous_sql:    Optional[str],
    steps:           list[ReasoningStep],
    t0:              float,
) -> ChatResponse:
    agent = SQLAgent(llm)

    try:
        raw_sql = agent.generate(question, previous_sql=previous_sql, history=history)
    except Exception as exc:
        log.exception("SQL agent failed")
        return ChatResponse(
            content=f"The SQL generator hit an error: *{exc}*. Please rephrase.",
            intent="factual", steps=steps, duration_ms=_ms(t0), error=str(exc),
        )

    if not raw_sql:
        # Check if the LLM failed due to daily token limit
        tpd_msg = llm.daily_limit_message
        return ChatResponse(
            content=tpd_msg or (
                "I couldn't translate your question into a query. "
                "If this was a follow-up, try restating the full question "
                "(e.g. 'Show TikTok spend by day' instead of 'now TikTok')."
            ),
            intent="factual", steps=steps, duration_ms=_ms(t0),
        )

    steps.append(ReasoningStep(label="Generate SQL",
                                detail="Schema-grounded SELECT generated."))

    # Check OUT_OF_SCOPE BEFORE validation — the sentinel has no table
    # reference by design and would otherwise fail the allowlist check.
    if _is_out_of_scope_sql(raw_sql):
        return _out_of_scope_response(question, "factual", steps, t0)

    try:
        sql = validate(raw_sql)
    except SQLValidationError as exc:
        steps.append(ReasoningStep(label="Validate SQL", detail=str(exc), status="error"))
        return ChatResponse(
            content=f"The generated query failed safety checks: *{exc}*. Please rephrase.",
            intent="factual", steps=steps, sql=raw_sql, duration_ms=_ms(t0),
        )
    steps.append(ReasoningStep(label="Validate SQL",
                                detail="Passed all 9 safety checks."))

    if "OUT_OF_SCOPE" in sql.upper():
        return _out_of_scope_response(question, "factual", steps, t0)

    # Execute with self-healing on recoverable BQ errors (syntax, bad columns)
    df, sql, bq_err = _execute_with_self_heal(
        sql=sql,
        original_question=question,
        llm=llm,
        system_prompt=prompts.SQL_AGENT_SYSTEM,
        max_bytes=config.MAX_BYTES_CHAT,
        history=history,
        steps=steps,
    )
    if bq_err is not None:
        # All retries exhausted (or error was non-recoverable)
        return ChatResponse(
            content=f"{bq_err.friendly}{(' ' + bq_err.hint) if bq_err.hint else ''}",
            intent="factual", steps=steps, sql=sql,
            duration_ms=_ms(t0), error=bq_err.technical[:300],
        )
    steps.append(ReasoningStep(label="Execute query",
                                detail=f"Returned {len(df)} rows."))

    if df.empty:
        return ChatResponse(
            content="I ran the query but got no results. Try broadening the question.",
            intent="factual", steps=steps, sql=sql, duration_ms=_ms(t0),
        )

    # Narrate — pass history so narrator references previous turns naturally
    narrate_msgs: list[dict] = []
    for m in history:
        narrate_msgs.append(m)
    narrate_msgs.append({
        "role": "user",
        "content": (
            f"Question: {question}\n\n"
            f"Result ({len(df)} rows):\n{df.head(20).to_markdown(index=False)}"
        ),
    })

    try:
        narrative = llm.complete(
            system_prompt=prompts.CHAT_NARRATOR_SYSTEM,
            messages=narrate_msgs,
            temperature=0.3,
            max_tokens=300,
        )
        narrative = _sanitize_narrative(narrative)
    except Exception as exc:
        log.exception("Narration failed")
        narrative = None

    steps.append(ReasoningStep(
        label="Narrate result",
        detail="Plain-English summary produced." if narrative else "LLM unavailable — raw data returned.",
        status="ok" if narrative else "pending",
    ))

    return ChatResponse(
        content=narrative or f"Here's what I found ({len(df)} rows). See the data preview below.",
        intent="factual", steps=steps, sql=sql, data=df, duration_ms=_ms(t0),
    )


# ── Chain-of-thought path (suggestion / analytical) ──────────────────────────

def _process_chain_of_thought(
    question:  str,
    llm:       LLMClient,
    history:   list[dict],
    intent:    str,
    steps:     list[ReasoningStep],
    t0:        float,
) -> ChatResponse:
    """
    For strategic questions ("how should we...", "why is X..."), the pipeline
    takes more steps: plan what data is needed, query it, then synthesize
    recommendations grounded in the actual data and conversation history.
    """
    # Step 2: Plan what data to gather (with history context)
    steps.append(ReasoningStep(
        label="Plan data gathering",
        detail="Strategic question — planning which data to query for context.",
    ))

    # Build messages: history → planner gets context of prior conversation
    planner_msgs: list[dict] = list(history)
    planner_msgs.append({"role": "user", "content": question})

    try:
        raw_sql = llm.complete(
            system_prompt=prompts.DATA_PLANNER_SYSTEM,
            messages=planner_msgs,
            max_tokens=500,
            temperature=0.1,
        )
    except Exception as exc:
        log.exception("Data planner failed")
        return ChatResponse(
            content=f"I couldn't plan the data gathering: *{exc}*. Please rephrase.",
            intent=intent, steps=steps, duration_ms=_ms(t0), error=str(exc),
        )

    if not raw_sql:
        tpd_msg = llm.daily_limit_message
        return ChatResponse(
            content=tpd_msg or "I couldn't plan a data query for that question. Please rephrase.",
            intent=intent, steps=steps, duration_ms=_ms(t0),
        )

    # Same pre-validation OUT_OF_SCOPE check as the factual path.
    if _is_out_of_scope_sql(raw_sql):
        return _out_of_scope_response(question, intent, steps, t0)

    # Step 3: Validate + execute (with fallback when planner generates bad SQL)
    sql: Optional[str] = None
    try:
        sql = validate(raw_sql)
        steps.append(ReasoningStep(label="Validate SQL", detail="Passed all 9 safety checks."))

    except SQLValidationError as exc:
        exc_lower = str(exc).lower()

        # Distinguish FALSE POSITIVES from genuine safety violations.
        #
        # False positive: the data planner generated CTEs, temp tables, or
        # complex SQL that tripped the DML keyword blocker — but the USER
        # didn't ask for anything dangerous. The question is legitimate (e.g.,
        # "what are the hidden insights from this data?"). In this case,
        # substitute a safe comprehensive fallback query so synthesis still
        # happens. Never surface a confusing "Write or admin operation detected"
        # error to the user for an innocent analytical question.
        #
        # Genuine safety violation: would come from the user INJECTING something
        # in their question (detected by sanitize_user_input earlier). These
        # should already be caught before we reach this point.
        is_planner_false_positive = (
            "write or admin" in exc_lower or
            "multiple select" in exc_lower or
            "statement count" in exc_lower or
            "comment" in exc_lower
        )

        if is_planner_false_positive:
            log.warning(
                "Data planner generated invalid SQL (false positive: %s). "
                "Using analytical fallback query.", exc
            )
            steps.append(ReasoningStep(
                label="Validate SQL",
                detail=(
                    "Planner generated a complex query that couldn't be validated — "
                    "using a comprehensive gold-mart query instead."
                ),
                status="pending",
            ))
            # Fallback: comprehensive per-campaign query gives the synthesizer
            # rich data for any broad analytical/insight question.
            fallback_sql = (
                f"SELECT platform, campaign_name, date,\n"
                f"       ROUND(SUM(spend), 2) AS spend,\n"
                f"       SUM(conversions) AS conversions,\n"
                f"       ROUND(SAFE_DIVIDE(SUM(spend), SUM(conversions)), 2) AS cpa,\n"
                f"       ROUND(SAFE_DIVIDE(SUM(clicks), SUM(impressions)) * 100, 2) AS ctr_pct,\n"
                f"       SUM(impressions) AS impressions,\n"
                f"       SUM(clicks) AS clicks\n"
                f"FROM `{config.GOLD_REF}`\n"
                f"GROUP BY date, platform, campaign_name\n"
                f"ORDER BY spend DESC\n"
                f"LIMIT {config.MAX_RESULT_ROWS}"
            )
            try:
                sql = validate(fallback_sql)
                steps[-1].status = "ok"
                steps[-1].detail += " Fallback query validated successfully."
            except SQLValidationError as fb_exc:
                # Should never happen — the fallback is hardcoded and safe.
                log.error("Analytical fallback SQL also failed validation: %s", fb_exc)
                steps.append(ReasoningStep(label="Validate SQL", detail=str(fb_exc), status="error"))
                return ChatResponse(
                    content="I couldn't gather data for that question. Try rephrasing.",
                    intent=intent, steps=steps, duration_ms=_ms(t0),
                )
        else:
            # Genuine safety concern — surface to the user
            steps.append(ReasoningStep(label="Validate SQL", detail=str(exc), status="error"))
            return ChatResponse(
                content=f"My data-gathering plan failed safety checks: *{exc}*.",
                intent=intent, steps=steps, sql=raw_sql, duration_ms=_ms(t0),
            )

    # Execute with self-healing on recoverable BQ errors (syntax, bad columns)
    df, sql, bq_err = _execute_with_self_heal(
        sql=sql,
        original_question=question,
        llm=llm,
        system_prompt=prompts.DATA_PLANNER_SYSTEM,
        max_bytes=config.MAX_BYTES_CHAT,
        history=history,
        steps=steps,
    )
    if bq_err is not None:
        return ChatResponse(
            content=f"{bq_err.friendly}{(' ' + bq_err.hint) if bq_err.hint else ''}",
            intent=intent, steps=steps, sql=sql,
            duration_ms=_ms(t0), error=bq_err.technical[:300],
        )
    steps.append(ReasoningStep(
        label="Gather context",
        detail=f"Retrieved {len(df)} rows of context for synthesis.",
    ))

    if df.empty:
        return ChatResponse(
            content="I planned a query for context but got no rows. The data may be incomplete.",
            intent=intent, steps=steps, sql=sql, duration_ms=_ms(t0),
        )

    # Step 4: Synthesize recommendations (history-aware)
    steps.append(ReasoningStep(
        label="Synthesize recommendations",
        detail="Generating data-grounded recommendations…",
    ))

    synth_msgs: list[dict] = list(history)
    synth_msgs.append({
        "role": "user",
        "content": (
            f"Question: {question}\n\n"
            f"Data gathered ({len(df)} rows):\n{df.head(30).to_markdown(index=False)}"
        ),
    })

    try:
        answer = llm.complete(
            system_prompt=prompts.SUGGESTION_SYNTHESIZER_SYSTEM,
            messages=synth_msgs,
            temperature=0.4,
            max_tokens=700,
            # Suggestion synthesis can be long — prefer larger-context model if available
            prefer_long_context=len(history) >= 3,
        )
        answer = _sanitize_narrative(answer)
    except Exception as exc:
        log.exception("Synthesizer failed")
        answer = None

    return ChatResponse(
        content=answer or (
            "I gathered the data but the recommendation generator is offline. "
            "Review the data table below to see the underlying numbers."
        ),
        intent=intent, steps=steps, sql=sql, data=df, duration_ms=_ms(t0),
    )


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ── Narrative sanitization (defense-in-depth against markdown leakage) ───────
#
# CHAT_NARRATOR_SYSTEM and SUGGESTION_SYNTHESIZER_SYSTEM both instruct the LLM
# to output plain prose with no markdown. LLMs sometimes ignore this and emit
# a stray inline-code span mid-sentence, e.g.:
#
#   "...the predicted spend is notably highest on TikTok, with an average
#    forecast of around `3.2M, while Google's predicted spend is around` 1.5M..."
#
# This breaks the sentence into prose / code / prose, rendering as a jarring
# monospace fragment in the middle of a paragraph. Since the prompt-level fix
# can't guarantee compliance, this function cleans up the output as a final
# safety net before it's shown to the user.

_UNMATCHED_BACKTICK_PAIR = re.compile(r"`([^`]*)`")


def _sanitize_narrative(text: Optional[str]) -> Optional[str]:
    """
    Remove stray markdown artifacts from narrator/synthesizer output.

    - Inline code spans (`...`) → unwrapped to plain text. The narrator
      prompt explicitly forbids backticks, so any backtick-wrapped content
      is almost certainly a formatting accident, not intentional code —
      unwrapping preserves the (likely numeric/text) content while removing
      the visual glitch.
    - Any leftover stray backtick characters are stripped entirely.
    - Bold/italic asterisks (**text** or *text*) are unwrapped similarly,
      since the narrator prompt also forbids markdown emphasis.

    Returns None unchanged (so callers' `narrative or fallback` logic still
    works) and leaves empty/whitespace-only strings as-is.
    """
    if text is None:
        return None

    # Unwrap inline code spans: `content` → content
    cleaned = _UNMATCHED_BACKTICK_PAIR.sub(r"\1", text)
    # Strip any remaining lone backticks (unmatched opens/closes)
    cleaned = cleaned.replace("`", "")

    # Unwrap bold/italic emphasis: **content** / *content* → content
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)

    return cleaned