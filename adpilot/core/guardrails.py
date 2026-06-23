"""Free, deterministic defenses that run before anything reaches the model or the database.

validate_sql      — defense-in-depth on model-written SQL (never trusts the model)
sanitize_question — trust boundary for user text (length, control chars, prompt injection)
is_in_scope       — cheap blocklist for obviously off-topic questions
Budget            — per-request SQL execution cap
RateLimiter       — process-level requests-per-minute bucket for the model API
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import sqlparse

from adpilot.core.errors import AdPilotError

MAX_SQL_LENGTH = 4000
MAX_QUESTION_LENGTH = 600

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|MERGE|TRUNCATE|GRANT|REVOKE|EXEC(?:UTE)?|CALL|"
    r"COPY|LOAD|EXPORT|IMPORT|BEGIN|COMMIT|ROLLBACK|DECLARE|ASSERT|RAISE|REPLACE\s+INTO|ATTACH|INSTALL|PRAGMA)\b",
    re.IGNORECASE,
)
_SUSPICIOUS = re.compile(
    r"(INFORMATION_SCHEMA|__TABLES__|__SCHEMA__|@@version|pg_catalog|sqlite_master|duckdb_\w+\(|read_\w+\()",
    re.IGNORECASE,
)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_FENCE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE)
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+[`\"]?([\w.\-]+)[`\"]?", re.IGNORECASE)
_CTE_NAME = re.compile(r"\b(?:WITH|,)\s*([\w]+)\s+AS\s*\(", re.IGNORECASE)
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)


def validate_sql(sql: str, allowed_tables: set[str], max_rows: int) -> str:
    """Return cleaned SQL (fences stripped, LIMIT enforced) or raise AdPilotError(kind="SqlPolicy")."""
    if not sql or not sql.strip():
        raise AdPilotError("SqlPolicy", "Empty SQL.")
    if len(sql) > MAX_SQL_LENGTH:
        raise AdPilotError("SqlPolicy", f"SQL is too long ({len(sql)} chars; max {MAX_SQL_LENGTH}).")

    sql = _FENCE.sub("", sql.strip()).strip().rstrip(";").strip()
    bare = _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))

    if _FORBIDDEN.search(bare) or _FORBIDDEN.search(sql):
        raise AdPilotError("SqlPolicy", "Only read-only SELECT statements are allowed.")
    if _SUSPICIOUS.search(bare):
        raise AdPilotError("SqlPolicy", "Metadata and file-reading functions are not allowed.")

    statements = [s for s in sqlparse.parse(bare) if str(s).strip()]
    if len(statements) != 1:
        raise AdPilotError("SqlPolicy", f"Exactly one statement is allowed (got {len(statements)}).")
    stmt_type = statements[0].get_type()
    if stmt_type not in ("SELECT", "UNKNOWN"):  # sqlparse reports WITH ... SELECT as UNKNOWN
        raise AdPilotError("SqlPolicy", f"Statement type {stmt_type} is not allowed; only SELECT.")

    ctes = {m.lower() for m in _CTE_NAME.findall(bare)}
    referenced = {t for t in _TABLE_REF.findall(bare) if t.lower() not in ctes}
    if not referenced:
        raise AdPilotError("SqlPolicy", "No table reference found.", hint=f"Allowed tables: {sorted(allowed_tables)}")
    unknown = sorted(t for t in referenced if t not in allowed_tables)
    if unknown:
        raise AdPilotError(
            "SqlPolicy",
            f"Table(s) not on the allowlist: {unknown}.",
            hint=f"Allowed tables: {sorted(allowed_tables)}",
        )

    m = _LIMIT.search(sql)
    if not m:
        return f"{sql} LIMIT {max_rows}"
    if int(m.group(1)) > max_rows:
        return _LIMIT.sub(f"LIMIT {max_rows}", sql)
    return sql


_INJECTION = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)|"
    r"forget\s+(everything|all|the\s+rules)|you\s+are\s+now\s+(a\s+)?(different|new)|"
    r"\bsystem\s*:|\[\[\s*system|<\s*system|act\s+as\s+(a\s+)?(developer|admin|root|sudo)|"
    r"jailbreak|dan\s+mode|developer\s+mode)",
    re.IGNORECASE,
)


def sanitize_question(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise AdPilotError("SqlPolicy", "Empty question.")
    if len(text) > MAX_QUESTION_LENGTH:
        raise AdPilotError("SqlPolicy", f"Question is too long ({len(text)} chars; max {MAX_QUESTION_LENGTH}).")
    text = "".join(c for c in text if c in "\n\t" or c >= " ").strip()
    if _INJECTION.search(text):
        raise AdPilotError("SqlPolicy", "The question contains instruction-override patterns. Please rephrase in plain language.")
    return text


_OFF_TOPIC = re.compile(
    r"\b(recipe|cook|bake|ingredient|meal|breakfast|lunch|dinner|"
    r"weather|temperature|rain|snow|sunny|"
    r"write.+(code|script|program|function|class|python|javascript|java|rust)|how.do.i.code|"
    r"poem|haiku|sonnet|limerick|story|joke|song|lyrics|essay|"
    r"horoscope|astrology|dating|relationship.advice|medical.advice|symptom|diagnos|"
    r"translate.+(to|from).+(spanish|french|german|chinese|japanese)|"
    r"solve.+(equation|integral|derivative|matrix)|"
    r"capital.of|population.of|who.invented|who.discovered|history.of.(?!.*marketing)|"
    r"play.+(game|chess|sudoku)|tell.+(joke|story))\b",
    re.IGNORECASE,
)


def is_in_scope(question: str) -> bool:
    """Blocklist, not allowlist: only obviously off-topic text is rejected before the model sees it."""
    return bool(question and question.strip()) and not _OFF_TOPIC.search(question)


@dataclass
class Budget:
    max_sql: int = 3
    sql_used: int = 0

    def take_sql(self) -> None:
        if self.sql_used >= self.max_sql:
            raise AdPilotError(
                "BudgetExceeded",
                f"SQL budget of {self.max_sql} executions per question is exhausted.",
                hint="Answer with the results you already have and state what is missing.",
            )
        self.sql_used += 1


@dataclass
class RateLimiter:
    """Blocking token bucket: at most `per_minute` acquisitions in any rolling 60 s window."""

    per_minute: int = 20
    _stamps: deque = field(default_factory=deque, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] >= 60:
                self._stamps.popleft()
            if len(self._stamps) >= self.per_minute:
                wait = 60 - (now - self._stamps[0])
                time.sleep(wait)
                now = time.monotonic()
            self._stamps.append(now)


# ponytail: one process-wide limiter; per-key buckets if the API ever serves several accounts
MODEL_RATE_LIMITER = RateLimiter(per_minute=20)
