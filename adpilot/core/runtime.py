"""What every surface (CLI, API, MCP, brief) does before it can call ask(), in one place.

The CLI used to own this. It moved here so a second surface cannot drift from the first on connector
precedence or on how strictly a missing audit store is treated.
"""

from __future__ import annotations

import os
import sys

from pydantic import BaseModel

from adpilot.connectors import get_connector
from adpilot.core import schema
from adpilot.core.agent import AnalystAnswer, refused
from adpilot.core.audit import AuditSink, AuditUnavailable, audit_config, build_sink
from adpilot.core.chart import ChartSpec
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import load_pack


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
