"""
lib/sql_validator.py — Hardened defense-in-depth SQL safety validator.

NEVER trusts the LLM. Each check is independent — defeating one does not
defeat the others. Each check has a clear, single responsibility.

Stack (in order, fail-fast):
   1. Length cap on the SQL string                  (anti-DOS)
   2. Markdown-fence strip                          (LLM output normalization)
   3. SQL-comment stripping for keyword detection   (catches DML hidden in /* */)
   4. Forbidden-keyword regex                       (DML / DDL / DCL / scripting)
   5. Suspicious-pattern regex                      (INFORMATION_SCHEMA etc.)
   6. Statement count                               (no semicolon-chaining)
   7. sqlparse statement-type check                 (must be SELECT)
   8. Table allowlist                               (only our 4 tables)
   9. LIMIT enforcement / clamp                     (cost cap)

The user-input safety helpers (anti-prompt-injection + HTML escape) also live
here since they're part of the same defensive surface.
"""
from __future__ import annotations

import html
import re

import sqlparse

from . import config

# ── Limits ─────────────────────────────────────────────────────────────────────
MAX_SQL_LENGTH      = 4000     # SELECTs we generate never exceed ~1000 chars
MAX_QUESTION_LENGTH = 600      # Chat input cap

# ── Forbidden SQL keywords ─────────────────────────────────────────────────────
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|MERGE|TRUNCATE|"
    r"GRANT|REVOKE|EXEC(?:UTE)?|CALL|"
    r"COPY|LOAD|EXPORT|IMPORT|"
    r"BEGIN|COMMIT|ROLLBACK|"
    r"DECLARE|ASSERT|RAISE|"
    r"REPLACE\s+INTO)\b",
    re.IGNORECASE,
)

# Patterns to remove before forbidden-keyword check (so hidden DML is detected)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT  = re.compile(r"--[^\n]*")

# Fully-qualified table references
_TABLE_REF = re.compile(r"`([a-zA-Z0-9_\-]+\.[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+)`")

# Markdown fences the LLM may emit despite the system prompt
_MD_FENCE_START = re.compile(r"^```(?:sql)?\s*", re.IGNORECASE)
_MD_FENCE_END   = re.compile(r"\s*```$")

# Metadata / cross-tenant probes
_SUSPICIOUS = re.compile(
    r"(INFORMATION_SCHEMA|__TABLES__|__SCHEMA__|"
    r"@@version|pg_catalog|sqlite_master)",
    re.IGNORECASE,
)


class SQLValidationError(Exception):
    """Raised when a query fails any safety check. Message is user-facing."""


def validate(sql: str) -> str:
    """Validate and clean SQL. Returns safe SQL; raises SQLValidationError otherwise."""
    if not sql or not sql.strip():
        raise SQLValidationError("Empty SQL")

    if len(sql) > MAX_SQL_LENGTH:
        raise SQLValidationError(
            f"SQL is suspiciously long ({len(sql)} chars). Maximum is {MAX_SQL_LENGTH}."
        )

    # Normalize: strip markdown fences and trailing semicolons
    sql = _MD_FENCE_START.sub("", sql).strip()
    sql = _MD_FENCE_END.sub("", sql).strip()
    sql = sql.rstrip(";").strip()

    # Strip comments BEFORE forbidden-keyword check
    sql_no_comments = _BLOCK_COMMENT.sub(" ", sql)
    sql_no_comments = _LINE_COMMENT.sub(" ", sql_no_comments)

    # Belt-and-suspenders: forbidden keywords are rejected whether they appear
    # in executable SQL or hidden inside comments. Legitimate questions never
    # contain words like DROP or DELETE.
    if _FORBIDDEN.search(sql_no_comments) or _FORBIDDEN.search(sql):
        raise SQLValidationError(
            "Write or admin operation detected — request rejected for safety."
        )

    if _SUSPICIOUS.search(sql_no_comments):
        raise SQLValidationError(
            "Metadata-table query rejected. Only campaign data is queryable."
        )

    parsed = sqlparse.parse(sql)
    if not parsed:
        raise SQLValidationError("SQL could not be parsed.")
    non_empty = [s for s in parsed if str(s).strip()]
    if len(non_empty) > 1:
        raise SQLValidationError(
            f"Multiple statements detected ({len(non_empty)}). Only one SELECT is permitted."
        )

    stmt_type = non_empty[0].get_type()
    if stmt_type is not None and stmt_type != "SELECT":
        raise SQLValidationError(
            f"Statement type '{stmt_type}' is not permitted. Only SELECT is allowed."
        )

    referenced = set(_TABLE_REF.findall(sql_no_comments))
    if not referenced:
        raise SQLValidationError(
            "No fully-qualified table reference found. Tables must be backtick-quoted "
            "as `project.dataset.table`."
        )
    for tbl in referenced:
        if tbl not in config.ALLOWED_TABLES:
            raise SQLValidationError(f"Table `{tbl}` is not on the allowlist.")

    return _enforce_limit(sql)


def _enforce_limit(sql: str) -> str:
    """Add LIMIT if missing; clamp it down if larger than MAX_RESULT_ROWS."""
    limit_pat = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)
    match = limit_pat.search(sql)
    if not match:
        return f"{sql} LIMIT {config.MAX_RESULT_ROWS}"
    current = int(match.group(1))
    if current > config.MAX_RESULT_ROWS:
        return limit_pat.sub(f"LIMIT {config.MAX_RESULT_ROWS}", sql)
    return sql


# ── User-input safety ─────────────────────────────────────────────────────────

_PROMPT_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)|"
    r"forget\s+(everything|all|the\s+rules)|"
    r"you\s+are\s+now\s+(a\s+)?(different|new)|"
    r"\bsystem\s*:|"
    r"\[\[\s*system|"
    r"<\s*system|"
    r"act\s+as\s+(a\s+)?(developer|admin|root|sudo)|"
    r"jailbreak|"
    r"dan\s+mode|"
    r"developer\s+mode)",
    re.IGNORECASE,
)


def sanitize_user_input(text: str) -> str:
    """Sanitize free-text input before sending to the LLM."""
    if not text:
        raise SQLValidationError("Empty question.")
    text = text.strip()
    if len(text) > MAX_QUESTION_LENGTH:
        raise SQLValidationError(
            f"Question is too long ({len(text)} chars). Maximum {MAX_QUESTION_LENGTH}."
        )
    # Strip ASCII control characters that have no business here
    text = "".join(c for c in text if c in "\n\t" or c >= " ")
    if _PROMPT_INJECTION_PATTERNS.search(text):
        raise SQLValidationError(
            "Your question contained patterns commonly used to bypass safety rules "
            "and has been rejected. Please rephrase using plain language."
        )
    return text


def html_escape_for_display(text: str) -> str:
    """HTML-escape a user string for safe insertion into Streamlit markdown."""
    return html.escape(text or "", quote=True)