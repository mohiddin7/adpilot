"""The function under test: run one eval case through agent.ask and record what happened."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pandas as pd
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

from adpilot.core.agent import AnalystAnswer, ask, refused
from adpilot.core.audit import RunContextInfo, error_kind_of, summarize_messages
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import Pack


class EvalInputs(BaseModel):
    name: str
    question: str | None = None
    turns: list[str] | None = None

    @property
    def final_question(self) -> str:
        return self.turns[-1] if self.turns else (self.question or "")


class Trace(BaseModel):
    answer: AnalystAnswer
    trace_id: str | None = None
    tool_calls: list[str] = []
    sql_attempted: list[str] = []
    sql_executed: list[str] = []
    rows_seen: list[dict] = []  # rows from every query the agent ran on the final turn (answer.data is only the last one)
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


def make_task(
    agent: Agent[AgentDeps, Any],
    deps_factory: Callable[[], AgentDeps],
    model_for: Callable[[EvalInputs], Model | None] | None = None,
    *,
    run_id: str | None = None,
    family_of: dict[str, str] | None = None,
) -> Callable[[EvalInputs], Trace]:
    def task(inputs: EvalInputs) -> Trace:
        deps = deps_factory()
        deps.run_context = RunContextInfo(source="eval", run_id=run_id, case_name=inputs.name, family=(family_of or {}).get(inputs.name))
        model = model_for(inputs) if model_for else None
        history: list[ModelMessage] = []
        turns = inputs.turns or [inputs.question or ""]
        start = time.perf_counter()
        trace = Trace(answer=AnalystAnswer(answer_md=""))
        for i, q in enumerate(turns):
            answer, new_messages, trace_id = ask(agent, deps, q, history=history or None, model=model)
            history.extend(new_messages)
            if i == len(turns) - 1:
                trace.answer = answer
                trace.trace_id = trace_id
                s = summarize_messages(new_messages)
                trace.model_calls, trace.tool_calls, trace.sql_attempted, trace.repairs = s.model_calls, s.tool_calls, s.sql_attempted, s.repairs
        trace.duration_s = round(time.perf_counter() - start, 3)
        trace.sql_executed = list(deps.connector.executed) if isinstance(deps.connector, RecordingSource) else []
        trace.rows_seen = [row for r in deps.results for row in r.rows[:30]]
        trace.refused = refused(trace.answer)
        trace.error_kind = error_kind_of(trace.answer.caveats)
        return trace

    return task
