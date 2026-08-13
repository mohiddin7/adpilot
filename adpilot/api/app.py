"""HTTP surface: transport in, ask() in the middle, transport out.

No guardrail, model-fallback or audit logic lives here — all of it is inside ask(). A rejected key never
reaches ask(), so a 401 writes no audit row; a refused *question* does reach it, so it returns 200 and is
recorded, exactly as the CLI shows it.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import secrets
import sys

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from adpilot.core.agent import ask, build_agent
from adpilot.core.audit import AuditSink, RunContextInfo
from adpilot.core.guardrails import Budget
from adpilot.core.models import build_model
from adpilot.core.runtime import AnswerBody, answer_body, build_deps, configure_tracing, open_sink
from adpilot.core.tools import AgentDeps

MIN_KEY_LEN = 24
log = logging.getLogger(__name__)


class AskRequest(BaseModel):
    # extra="forbid" so a `connector` field is rejected rather than ignored: the data source a deployment
    # reads is server-side configuration, never something a caller can repoint.
    model_config = {"extra": "forbid"}

    question: str = Field(min_length=1, max_length=4000)  # tier 1; sanitize_question enforces 600 inside ask()
    session_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


def request_deps(app: FastAPI) -> AgentDeps:
    """A per-request copy: the connector, pack, schema text and sink are shared; per-turn state is not.

    ask() also resets budget/results, but a fresh list object per request means two concurrent requests can
    never see each other's rows even before ask() runs.
    """
    return dataclasses.replace(
        app.state.deps_template, budget=Budget(), last_result=None, results=[], run_context=None
    )


def flush_audit(sink: AuditSink) -> None:
    rep = sink.flush()
    if not rep.ok:
        log.warning(
            "audit: %d row(s) not persisted, retrying on the next request: %s",
            rep.pending, "; ".join(rep.errors),
        )


def _api_key() -> str:
    key = os.environ.get("ADPILOT_API_KEY", "")
    if len(key) < MIN_KEY_LEN:
        raise RuntimeError(
            f"ADPILOT_API_KEY must be set and at least {MIN_KEY_LEN} characters — refusing to serve"
        )
    return key


def create_app(pack: str = "ads", connector: str | None = None) -> FastAPI:
    load_dotenv()
    configure_tracing()
    api_key = _api_key()
    sink = open_sink(sys.stderr)
    if sink is None:
        raise RuntimeError("audit store unreachable — refusing to serve (ADPILOT_AUDIT=memory to run unrecorded)")

    app = FastAPI(title="AdPilot API")
    app.state.sink = sink
    app.state.agent = build_agent(build_model())
    # schema.summary() queries the data source, so the connector and schema text are built once and shared;
    # per-request deps are a copy with fresh per-turn state.
    app.state.deps_template = build_deps(pack, connector, sink)

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if not x_api_key or not secrets.compare_digest(x_api_key, api_key):
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/healthz")
    def healthz() -> dict:
        # Unauthenticated: it says the process is up and nothing else — not the pack, connector or model chain.
        return {"status": "ok"}

    @app.get("/schema", response_class=PlainTextResponse, dependencies=[Depends(require_key)])
    def get_schema() -> str:
        return app.state.deps_template.schema_text

    @app.post("/ask", response_model=AnswerBody, dependencies=[Depends(require_key)])
    def post_ask(req: AskRequest, background: BackgroundTasks) -> AnswerBody:
        deps = request_deps(app)
        deps.run_context = RunContextInfo(source="api", session_id=req.session_id)
        history = sink.load_session(req.session_id) if req.session_id else None
        answer, _messages, trace_id = ask(app.state.agent, deps, req.question, history=history)
        # ask() has already buffered the record; flushing after the response keeps a BigQuery load job
        # out of the caller's latency. A failure leaves the rows buffered for the next request.
        background.add_task(flush_audit, sink)
        return answer_body(answer, trace_id)

    return app
