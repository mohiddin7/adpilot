"""What every surface (CLI, API, MCP, brief) does before it can call ask(), in one place.

The CLI used to own this. It moved here so a second surface cannot drift from the first on connector
precedence or on how strictly a missing audit store is treated.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import logging
import os
import sys
from datetime import UTC, datetime

from pydantic import BaseModel

from adpilot.connectors import get_connector
from adpilot.core import schema
from adpilot.core.agent import AnalystAnswer, refused
from adpilot.core.audit import (
    AuditSink,
    AuditUnavailable,
    RunContextInfo,
    audit_config,
    build_record,
    build_sink,
    new_trace_id,
)
from adpilot.core.chart import ChartSpec
from adpilot.core.guardrails import Budget
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import load_pack

log = logging.getLogger(__name__)

# Transport limits every non-CLI surface applies before ask() — the first of the two size tiers. Over them is an
# unaudited reject (422 / tool error). The semantic 600-char cap is sanitize_question's, inside ask(), and is audited.
QUESTION_MAX_CHARS = 4000
# session_id is a BigQuery query parameter and an SSE field: bound it at the trust boundary even though the
# query is parameterised.
SESSION_ID_PATTERN = r"^[A-Za-z0-9._-]+$"
SESSION_ID_MAX_CHARS = 64


def build_deps(pack_name: str, connector_name: str | None, audit: AuditSink) -> AgentDeps:
    """Explicit argument → ADPILOT_CONNECTOR → the pack's default. schema.summary() hits the data source."""
    pack = load_pack(pack_name)
    name = connector_name or os.environ.get("ADPILOT_CONNECTOR") or pack.raw["connector"]
    connector = get_connector(name, pack)
    return AgentDeps(connector=connector, pack=pack, schema_text=schema.summary(connector, pack), audit=audit)


def announce_audit(mode: str, project: str, dataset: str, out) -> None:
    """One-line audit-target notice. `mode == "memory"` is always announced — nothing else says so."""
    if mode == "memory":
        print("audit: memory — this session is NOT recorded (ADPILOT_AUDIT=memory)", file=out)
    else:
        print(f"audit: bigquery {project}.{dataset}", file=out)


def open_sink(out) -> AuditSink | None:
    """Strict preflight: refuse to run unrecorded. Memory mode is allowed only when asked for, and says so."""
    cfg = audit_config()
    sink = build_sink(cfg)
    if cfg.mode == "memory":
        announce_audit(cfg.mode, cfg.project, cfg.dataset, out)
        return sink
    try:
        sink.preflight()
    except AuditUnavailable as exc:
        print(f"audit unavailable ({exc.kind}): {exc.hint}", file=out)
        return None
    announce_audit(cfg.mode, cfg.project, cfg.dataset, out)
    return sink


def configure_tracing() -> None:
    """Live traces are opt-in: a Logfire token turns on pydantic-ai's OTel instrumentation."""
    if not os.environ.get("LOGFIRE_TOKEN"):
        return
    try:
        import logfire
    except ImportError:
        print("LOGFIRE_TOKEN is set but logfire is not installed: pip install 'adpilot[logfire]'", file=sys.stderr)
        return
    logfire.configure()
    logfire.instrument_pydantic_ai()


def fresh_deps(template: AgentDeps) -> AgentDeps:
    """A per-call copy: the connector, pack, schema text and sink are shared; per-turn state is not.

    ask() also resets budget/results, but a fresh list object per call means two concurrent calls can never see
    each other's rows even before ask() runs. Sharing the connector is safe: tools._EXEC_LOCK serialises queries.
    """
    return dataclasses.replace(template, budget=Budget(), last_result=None, results=[], run_context=None)


def flush_audit(sink: AuditSink) -> bool:
    """Persist buffered rows; never raises. True when everything is persisted; a failure leaves rows buffered."""
    try:
        rep = sink.flush()
    except Exception:  # never let a flush failure escape: callers run it in a finally or after the response
        log.exception("audit: flush raised, row(s) stay buffered")
        return False
    if not rep.ok:
        log.warning(
            "audit: %d row(s) not persisted, retrying on the next request: %s",
            rep.pending, "; ".join(rep.errors),
        )
    return rep.ok


def record_schema_read(sink: AuditSink, pack_name: str, source: str) -> None:
    """A schema read is not an agent call, but it is an authenticated surface call, so it gets one row too.

    `case_name = 'schema'` is how queries tell these rows from answers (the question and answer are empty markers,
    not the schema text: it is the same large string every time and already in the pack).
    """
    sink.record(build_record(
        trace_id=new_trace_id(), ts=datetime.now(UTC), latency_s=0.0, question="(schema)", answer_md="", sql=None,
        refused=False, confidence=1.0, caveats=[], messages=[], usage=None, model_requested=None,
        context=RunContextInfo(source=source, case_name="schema"), pack_name=pack_name, prompt_hash=None,
    ))


def mcp_installed() -> bool:
    """`mcp` is an optional extra: an api-only or CLI-only install must still run, and say what is missing.

    Checked explicitly rather than by catching ImportError, so a real import bug inside adpilot.mcp_server
    fails loudly instead of silently switching the MCP surface off.
    """
    return importlib.util.find_spec("mcp") is not None


class AnswerBody(BaseModel):
    """The wire shape every non-CLI surface returns.

    It lives here rather than in adpilot/api/app.py because `api` and `mcp` are separate optional extras:
    an MCP-only install has no FastAPI, so the shared shape must sit in a framework-free module.
    """

    trace_id: str
    answer_md: str
    sql: str | None = None
    data: list[dict] | None = None
    chart: ChartSpec | None = None
    confidence: float = 1.0
    caveats: list[str] = []
    refused: bool = False


def answer_body(answer: AnalystAnswer, trace_id: str) -> AnswerBody:
    """`refused` is agent.refused(), so every surface agrees with the CLI and the eval harness on what a refusal is."""
    return AnswerBody(
        trace_id=trace_id,
        answer_md=answer.answer_md,
        sql=answer.sql,
        data=answer.data,
        chart=answer.chart,
        confidence=answer.confidence,
        caveats=answer.caveats,
        refused=refused(answer),
    )
