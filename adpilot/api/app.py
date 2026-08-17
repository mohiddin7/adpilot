"""HTTP surface: transport in, ask() in the middle, transport out.

No guardrail, model-fallback or audit logic lives here — all of it is inside ask(). A rejected key never
reaches ask(), so a 401 writes no audit row; a refused *question* does reach it, so it returns 200 and is
recorded, exactly as the CLI shows it.

One documented exception to "ask() owns redaction": the `detail` of a `sql` status event is the SQL the model
*attempted*, taken from the tool-call event before ask() ever sees a result, so it is the one text a surface
emits that has not passed redact_output — including a query the validator went on to reject.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import math
import os
import queue
import secrets
import sys

import anyio.to_thread
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from adpilot.core.agent import ask, build_agent
from adpilot.core.audit import AuditSink, RunContextInfo
from adpilot.core.guardrails import Budget, RateLimiter
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
    try:
        rep = sink.flush()
    except Exception:  # never let a flush failure escape: on the SSE path it runs in run()'s finally,
        log.exception("audit: flush raised, row(s) stay buffered")  # outside the try that maps run errors
        return
    if not rep.ok:
        log.warning(
            "audit: %d row(s) not persisted, retrying on the next request: %s",
            rep.pending, "; ".join(rep.errors),
        )


def status_event(ev) -> dict | None:
    """pydantic-ai stream event → one SSE `status` payload, or None for events a client does not need."""
    from pydantic_ai.messages import FinalResultEvent, FunctionToolCallEvent, FunctionToolResultEvent

    if isinstance(ev, FunctionToolCallEvent):
        name = ev.part.tool_name
        detail = None
        if name == "run_sql":
            args = ev.part.args_as_dict() if hasattr(ev.part, "args_as_dict") else (ev.part.args or {})
            detail = str((args or {}).get("sql", ""))[:400] or None
        return {"phase": "sql" if name == "run_sql" else "tool", "tool": name, "detail": detail}
    if isinstance(ev, FunctionToolResultEvent):
        content = getattr(ev, "content", None) or getattr(ev.part, "content", None)
        if type(content).__name__ == "SqlError":
            # The model is about to repair its own query — the one progress event users actually want to see.
            return {"phase": "repair", "tool": "run_sql", "detail": getattr(content, "kind", None)}
        return None
    if isinstance(ev, FinalResultEvent):
        return {"phase": "answering", "tool": None, "detail": None}
    return None


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

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # Cloud Run runs with --min-instances 0: a BackgroundTasks flush is not guaranteed to get CPU after
        # the response, so the last rows would die with the instance. flush_audit logs whatever it cannot persist.
        flush_audit(sink)

    app = FastAPI(title="AdPilot API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    model = build_model()
    if model is None:
        log.warning("no AGENT_LLM_BEARER_TOKEN — answering from pre-defined queries only, every question")
    app.state.agent = build_agent(model)
    app.state.limiter = RateLimiter(per_minute=int(os.environ.get("ADPILOT_API_RPM", "20")))
    # schema.summary() queries the data source, so the connector and schema text are built once and shared;
    # per-request deps are a copy with fresh per-turn state.
    app.state.deps_template = build_deps(pack, connector, sink)

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        # isascii() first: Starlette decodes header bytes as latin-1, and compare_digest raises TypeError
        # (→ 500 traceback for an unauthenticated caller) on a str with any code point above 0x7F.
        if not x_api_key or not x_api_key.isascii() or not secrets.compare_digest(x_api_key, api_key):
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
        wait = app.state.limiter.try_acquire()
        if wait:
            raise HTTPException(
                status_code=429, detail="rate limit exceeded",
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )
        deps = request_deps(app)
        deps.run_context = RunContextInfo(source="api", session_id=req.session_id)
        history = sink.load_session(req.session_id) if req.session_id else None
        answer, _messages, trace_id = ask(app.state.agent, deps, req.question, history=history)
        # ask() has already buffered the record; flushing after the response keeps a BigQuery load job
        # out of the caller's latency. A failure leaves the rows buffered for the next request.
        background.add_task(flush_audit, sink)
        return answer_body(answer, trace_id)

    @app.get("/ask/stream", dependencies=[Depends(require_key)])
    async def ask_stream(
        question: str = Query(min_length=1, max_length=4000),
        session_id: str | None = Query(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
    ) -> StreamingResponse:
        wait = app.state.limiter.try_acquire()
        if wait:
            raise HTTPException(
                status_code=429, detail="rate limit exceeded",
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )
        events: queue.Queue = queue.Queue()
        deps = request_deps(app)
        deps.run_context = RunContextInfo(source="api", session_id=session_id)
        # A BigQuery query on the event loop stalls every other request and every other stream's keepalive;
        # anyio's threadpool (40 slots, the one /ask itself runs in) rather than the loop's default executor,
        # which is min(32, cpu+4) — 5 threads on a 1-vCPU instance against a documented --concurrency 4.
        history = await anyio.to_thread.run_sync(sink.load_session, session_id) if session_id else None

        async def handler(ctx, stream) -> None:
            async for ev in stream:
                payload = status_event(ev)
                if payload:
                    events.put(("status", payload))

        def run() -> None:
            try:
                answer, _messages, trace_id = ask(
                    app.state.agent, deps, question, history=history, event_stream_handler=handler
                )
                events.put(("answer", answer_body(answer, trace_id).model_dump(mode="json")))
            except Exception as exc:  # ask() maps its own failures; anything reaching here is transport or threading
                log.exception("stream run failed")
                events.put(("error", {"kind": exc.__class__.__name__, "message": str(exc)[:200]}))
            finally:
                events.put(("done", {}))
                flush_audit(sink)

        async def body():
            # Emitted before the run starts, so a client shows progress immediately and the first event is
            # deterministic even on the rule-based path, which makes no model call and so emits no events.
            yield "event: status\ndata: " + json.dumps({"phase": "thinking", "tool": None, "detail": None}) + "\n\n"
            task = asyncio.create_task(anyio.to_thread.run_sync(run))
            while True:
                try:
                    name, payload = await anyio.to_thread.run_sync(events.get, True, 15)
                except queue.Empty:
                    yield ": keepalive\n\n"  # stops proxies buffering a slow run
                    continue
                yield f"event: {name}\ndata: {json.dumps(payload)}\n\n"
                if name == "done":
                    break
            # Normal path: wait for the worker so the response ends only once the run is done. On a client
            # disconnect this line is never reached — cancellation unwinds the generator at the events.get
            # await above — and it does not need to be: run() executes in an OS thread (asyncio.to_thread), and
            # cancelling an await cannot stop a running thread, so ask()'s buffered record still reaches
            # flush_audit() in run()'s finally. Pinned by test_a_client_disconnect_still_records_the_run.
            await task

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
