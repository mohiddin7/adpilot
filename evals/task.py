"""The function under test: run one eval case through agent.ask and record what happened."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import pandas as pd
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

from adpilot.core.agent import REFUSAL_TEXT, AnalystAnswer, ask
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import Pack

log = logging.getLogger(__name__)


class EvalInputs(BaseModel):
    name: str
    question: str | None = None
    turns: list[str] | None = None

    @property
    def final_question(self) -> str:
        return self.turns[-1] if self.turns else (self.question or "")


class Trace(BaseModel):
    answer: AnalystAnswer
    tool_calls: list[str] = []
    sql_attempted: list[str] = []
    sql_executed: list[str] = []
    repairs: int = 0
    model_calls: int = 0
    refused: bool = False
    error_kind: str | None = None
    duration_s: float = 0.0


class RecordingSource:
    """DataSource wrapper: records every statement that reaches the database."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.executed: list[str] = []

    @property
    def dialect(self) -> str:
        return self._inner.dialect

    def query(self, sql: str, max_bytes: int | None = None) -> pd.DataFrame:
        self.executed.append(sql)
        return self._inner.query(sql, max_bytes=max_bytes)

    def list_tables(self) -> list[str]:
        return self._inner.list_tables()

    def columns(self, table: str) -> list[tuple[str, str]]:
        return self._inner.columns(table)


def load_fixtures(connector: Any, pack: Pack) -> None:
    """Populate the empty tier-2 tables so tool-family cases have data (DuckDB only)."""
    path = pack.root / "eval_fixtures.sql"
    if hasattr(connector, "execute_script") and path.exists():
        connector.execute_script(path.read_text())


_ERROR_KINDS = ("OutOfScope", "SqlPolicy", "BudgetExceeded", "ModelRateLimited", "ModelUnavailable", "DataSourceUnavailable")


def _summarize(messages: list[ModelMessage], trace: Trace) -> None:
    for m in messages:
        if m.kind == "response":
            trace.model_calls += 1
            for p in m.parts:
                if p.part_kind == "tool-call" and not p.tool_name.startswith("final_result"):
                    trace.tool_calls.append(p.tool_name)
                    if p.tool_name == "run_sql":
                        args = p.args_as_dict() if hasattr(p, "args_as_dict") else p.args
                        trace.sql_attempted.append(str((args or {}).get("sql", "")))
        else:
            for p in m.parts:
                if p.part_kind == "tool-return" and getattr(p.content, "__class__", type(None)).__name__ == "SqlError":
                    trace.repairs += 1


def make_task(
    agent: Agent[AgentDeps, Any],
    deps_factory: Callable[[], AgentDeps],
    model_for: Callable[[EvalInputs], Model | None] | None = None,
) -> Callable[[EvalInputs], Trace]:
    def task(inputs: EvalInputs) -> Trace:
        deps = deps_factory()
        model = model_for(inputs) if model_for else None
        history: list[ModelMessage] = []
        turns = inputs.turns or [inputs.question or ""]
        start = time.perf_counter()
        trace = Trace(answer=AnalystAnswer(answer_md=""))
        for i, q in enumerate(turns):
            log.debug("case %s: turn %d/%d %r", inputs.name, i + 1, len(turns), q)
            answer, new_messages = ask(agent, deps, q, history=history or None, model=model)
            history.extend(new_messages)
            if i == len(turns) - 1:
                trace.answer = answer
                _summarize(list(new_messages), trace)
        trace.duration_s = round(time.perf_counter() - start, 3)
        trace.sql_executed = list(deps.connector.executed) if isinstance(deps.connector, RecordingSource) else []
        caveats = trace.answer.caveats
        blocked_by_guard = caveats == ["SqlPolicy"] and trace.answer.confidence == 0.0  # sanitize_question rejected it
        trace.refused = "OutOfScope" in caveats or trace.answer.answer_md == REFUSAL_TEXT or blocked_by_guard
        trace.error_kind = next((c.split(":")[0] for c in trace.answer.caveats if c.split(":")[0] in _ERROR_KINDS), None)
        return trace

    return task
