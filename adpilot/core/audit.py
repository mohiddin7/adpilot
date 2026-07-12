"""Audit trail: one record per agent call, grades as separate rows, and the sinks that store them.

The permanent store is BigQuery (adpilot/core/audit_bigquery.py); MemorySink is for tests and the deterministic
eval tier. Nothing here does I/O except the sinks' flush()/preflight(); ask() only appends to a buffer.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Protocol

from pydantic import BaseModel
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

SCHEMA_VERSION = 1
DEFAULT_PROJECT = "adpilot-lakehouse"
DEFAULT_DATASET = "adpilot_audit"
ERROR_KINDS = ("OutOfScope", "SqlPolicy", "BudgetExceeded", "ModelRateLimited", "ModelUnavailable", "DataSourceUnavailable", "JudgeError")


# ---------- message walk (the one implementation; evals.task.Trace is filled from it) ----------


class MessageSummary(BaseModel):
    model_used: str | None = None
    model_calls: int = 0
    tool_calls: list[str] = []
    sql_attempted: list[str] = []
    repairs: int = 0
    provider_response_ids: list[str] = []


def summarize_messages(messages: Sequence[ModelMessage]) -> MessageSummary:
    s = MessageSummary()
    for m in messages:
        if m.kind == "response":
            s.model_calls += 1
            if m.model_name:
                s.model_used = m.model_name
            if m.provider_response_id:
                s.provider_response_ids.append(m.provider_response_id)
            for p in m.parts:
                if p.part_kind == "tool-call" and not p.tool_name.startswith("final_result"):
                    s.tool_calls.append(p.tool_name)
                    if p.tool_name == "run_sql":
                        args = p.args_as_dict() if hasattr(p, "args_as_dict") else p.args
                        s.sql_attempted.append(str((args or {}).get("sql", "")))
        else:
            for p in m.parts:
                if p.part_kind == "tool-return" and getattr(p.content, "__class__", type(None)).__name__ == "SqlError":
                    s.repairs += 1
    return s


def primary_model_name(model: Model | str | None) -> str | None:
    """The model the caller *asked for*: the first model of a FallbackModel, unwrapped through WrapperModels."""
    if model is None:
        return None
    if isinstance(model, str):
        return model
    models = getattr(model, "models", None)  # FallbackModel
    if models:
        return primary_model_name(models[0])
    return getattr(model, "model_name", None)


def error_kind_of(caveats: Sequence[str]) -> str | None:
    for c in caveats:
        kind = c.split(":")[0].strip()
        if kind in ERROR_KINDS:
            return kind
    return None


# ---------- records ----------


class RunContextInfo(BaseModel):
    source: str = "chat"  # chat | eval | judge
    session_id: str | None = None
    run_id: str | None = None
    case_name: str | None = None
    family: str | None = None


def _json_row(model: BaseModel, json_fields: tuple[str, ...]) -> dict:
    row = model.model_dump(mode="json")
    for f in json_fields:
        v = row.get(f)
        if v is not None and not isinstance(v, str):
            row[f] = json.dumps(v)
    return row


class AuditRecord(BaseModel):
    trace_id: str
    otel_trace_id: str | None = None
    ts: datetime
    environment: str
    source: str
    session_id: str | None = None
    run_id: str | None = None
    case_name: str | None = None
    family: str | None = None
    pack: str
    git_sha: str | None = None
    prompt_hash: str | None = None
    question: str
    answer_md: str
    sql: str | None = None
    refused: bool = False
    confidence: float = 1.0
    caveats: list[str] = []
    error_kind: str | None = None
    model_requested: str | None = None
    model_used: str | None = None
    fell_back: bool = False
    requests: int = 0
    tool_calls: list[str] = []
    repairs: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    messages_json: str = "[]"
    attributes: dict[str, Any] = {}
    schema_version: int = SCHEMA_VERSION

    def row(self) -> dict:
        return _json_row(self, ("attributes",))


class ScoreRow(BaseModel):
    trace_id: str
    run_id: str | None = None
    name: str
    value: float | None = None
    passed: bool | None = None
    source: str = "code"  # code | judge | human
    grader: str | None = None
    reason: str | None = None
    ts: datetime
    attributes: dict[str, Any] = {}
    schema_version: int = SCHEMA_VERSION

    def row(self) -> dict:
        return _json_row(self, ("attributes",))


class RunRow(BaseModel):
    run_id: str
    ts: datetime
    environment: str
    tier: str
    git_sha: str | None = None
    prompt_hash: str | None = None
    pack: str
    models: str = "{}"
    case_count: int = 0
    overall: float = 0.0
    gate_ok: bool = False
    gate_reasons: list[str] = []
    scorecard_json: str = "{}"
    invariants_json: str = "[]"
    calls_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    attributes: dict[str, Any] = {}
    schema_version: int = SCHEMA_VERSION

    def row(self) -> dict:
        return _json_row(self, ("attributes",))


# ---------- sinks ----------


@dataclass
class FlushReport:
    written: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def pending(self) -> int:
        return sum(self.failed.values())


class AuditUnavailable(RuntimeError):
    """The audit store cannot be used; the message carries the fix."""

    def __init__(self, kind: str, hint: str) -> None:
        super().__init__(f"{kind}: {hint}")
        self.kind = kind
        self.hint = hint


class AuditSink(Protocol):
    def preflight(self) -> None: ...
    def record(self, rec: AuditRecord) -> None: ...
    def add_scores(self, rows: Sequence[ScoreRow]) -> None: ...
    def add_run(self, row: RunRow) -> None: ...
    def pending_calls(self) -> list[AuditRecord]: ...
    def flush(self) -> FlushReport: ...
    def load_session(self, session_id: str, turns: int = 3) -> list[ModelMessage]: ...
    def list_runs(self, limit: int = 20) -> list[dict]: ...
    def export_run(self, run_id: str) -> Iterator[dict]: ...


class MemorySink:
    """Keeps everything in lists. flush() marks the buffer written. Tests + deterministic tier only."""

    def __init__(self) -> None:
        self.calls: list[AuditRecord] = []
        self.scores: list[ScoreRow] = []
        self.runs: list[RunRow] = []
        self.flushed: dict[str, int] = {"agent_calls": 0, "scores": 0, "eval_runs": 0}
        self._pending: dict[str, int] = {"agent_calls": 0, "scores": 0, "eval_runs": 0}
        self._lock = threading.Lock()

    def preflight(self) -> None:
        return None

    def record(self, rec: AuditRecord) -> None:
        with self._lock:
            self.calls.append(rec)
            self._pending["agent_calls"] += 1

    def add_scores(self, rows: Sequence[ScoreRow]) -> None:
        with self._lock:
            self.scores.extend(rows)
            self._pending["scores"] += len(rows)

    def add_run(self, row: RunRow) -> None:
        with self._lock:
            self.runs.append(row)
            self._pending["eval_runs"] += 1

    def pending_calls(self) -> list[AuditRecord]:
        return self.calls[len(self.calls) - self._pending["agent_calls"]:] if self._pending["agent_calls"] else []

    def flush(self) -> FlushReport:
        with self._lock:
            written = {k: v for k, v in self._pending.items() if v}
            for k, v in written.items():
                self.flushed[k] += v
                self._pending[k] = 0
        return FlushReport(written=written)

    def load_session(self, session_id: str, turns: int = 3) -> list[ModelMessage]:
        recs = [r for r in self.calls if r.session_id == session_id and r.source == "chat"]
        recs.sort(key=lambda r: r.ts)
        history: list[ModelMessage] = []
        for r in recs[-turns:]:
            history.extend(ModelMessagesTypeAdapter.validate_json(r.messages_json))
        return history

    def list_runs(self, limit: int = 20) -> list[dict]:
        return [r.row() for r in sorted(self.runs, key=lambda r: r.ts, reverse=True)[:limit]]

    def export_run(self, run_id: str) -> Iterator[dict]:
        by_trace: dict[str, list[dict]] = {}
        for s in self.scores:
            by_trace.setdefault(s.trace_id, []).append(s.row())
        for r in self.calls:
            if r.run_id == run_id:
                yield {**r.row(), "scores": by_trace.get(r.trace_id, [])}


# ---------- config + environment helpers ----------


@dataclass
class AuditConfig:
    mode: str  # bigquery | memory
    project: str
    dataset: str
    location: str | None
    service_account_json: str | None
    credentials_path: str | None


def audit_config(env: Mapping[str, str] | None = None) -> AuditConfig:
    env = os.environ if env is None else env
    return AuditConfig(
        mode="memory" if env.get("ADPILOT_AUDIT", "").lower() == "memory" else "bigquery",
        project=env.get("BQ_PROJECT_ID") or DEFAULT_PROJECT,
        dataset=env.get("BQ_AUDIT_DATASET") or DEFAULT_DATASET,
        location=env.get("BQ_LOCATION") or None,
        service_account_json=env.get("GCP_SERVICE_ACCOUNT_JSON") or None,
        credentials_path=env.get("GOOGLE_APPLICATION_CREDENTIALS") or None,
    )


def environment(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if env.get("GITHUB_ACTIONS"):
        return "ci"
    if env.get("STREAMLIT_RUNTIME") or env.get("K_SERVICE"):
        return "cloud"
    return "local"


@lru_cache(maxsize=1)
def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.environ.get("GITHUB_SHA") or None


def otel_trace_id() -> str | None:
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        return format(ctx.trace_id, "032x") if ctx.is_valid else None
    except Exception:  # noqa: BLE001 — tracing is optional; never let it break a call
        return None


def new_trace_id() -> str:
    return uuid.uuid4().hex


def host_attributes() -> dict[str, Any]:
    return {"host": socket.gethostname()}


def build_record(
    *,
    trace_id: str,
    ts: datetime,
    latency_s: float,
    question: str,
    answer_md: str,
    sql: str | None,
    refused: bool,
    confidence: float,
    caveats: Sequence[str],
    messages: Sequence[ModelMessage],
    usage: Any | None,
    model_requested: str | None,
    context: RunContextInfo | None,
    pack_name: str,
    prompt_hash: str | None,
    extra_attributes: dict[str, Any] | None = None,
) -> AuditRecord:
    s = summarize_messages(messages)
    ctx = context or RunContextInfo()
    cost = getattr(usage, "cost", None) if usage else None
    attributes = {**host_attributes(), "provider_response_ids": s.provider_response_ids, **(extra_attributes or {})}
    return AuditRecord(
        trace_id=trace_id,
        otel_trace_id=otel_trace_id(),
        ts=ts,
        environment=environment(),
        source=ctx.source,
        session_id=ctx.session_id,
        run_id=ctx.run_id,
        case_name=ctx.case_name,
        family=ctx.family,
        pack=pack_name,
        git_sha=git_sha(),
        prompt_hash=prompt_hash,
        question=question,
        answer_md=answer_md,
        sql=sql,
        refused=refused,
        confidence=confidence,
        caveats=list(caveats),
        error_kind=error_kind_of(caveats),
        model_requested=model_requested,
        model_used=s.model_used,
        fell_back=bool(s.model_used and model_requested and s.model_used != model_requested),
        requests=int(getattr(usage, "requests", 0) or 0) if usage else 0,
        tool_calls=s.tool_calls,
        repairs=s.repairs,
        tokens_in=int(getattr(usage, "input_tokens", 0) or 0) if usage else 0,
        tokens_out=int(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
        cost_usd=float(cost) if cost is not None else 0.0,
        latency_s=round(latency_s, 3),
        messages_json=ModelMessagesTypeAdapter.dump_json(list(messages)).decode() if messages else "[]",
        attributes=attributes,
    )


def build_sink(cfg: AuditConfig) -> AuditSink:
    if cfg.mode == "memory":
        return MemorySink()
    from adpilot.core.audit_bigquery import BigQuerySink

    return BigQuerySink(cfg)
