"""HTTP surface: transport in, ask() in the middle, transport out.

No guardrail, model-fallback or audit logic lives here — all of it is inside ask(). A rejected key never
reaches ask(), so a 401 writes no audit row; a refused *question* does reach it, so it returns 200 and is
recorded, exactly as the CLI shows it.

One documented exception to "ask() owns redaction": the `detail` of a `sql` status event is the SQL the model
*attempted*, taken from the tool-call event before ask() ever sees a result, so it is the one text a surface
emits that has not passed redact_output — including a query the validator went on to reject.

/mcp serves the same two MCP tools as `adpilot mcp` (adpilot/mcp_server.py) over streamable HTTP. It is a raw
ASGI route, which FastAPI's Depends cannot reach, so KeyGate applies the identical key check.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers

from adpilot.core.agent import ask, build_agent
from adpilot.core.audit import RunContextInfo
from adpilot.core.guardrails import RateLimiter
from adpilot.core.models import build_model
from adpilot.core.runtime import (
    QUESTION_MAX_CHARS,
    SESSION_ID_MAX_CHARS,
    SESSION_ID_PATTERN,
    AnswerBody,
    answer_body,
    build_deps,
    configure_tracing,
    flush_audit,
    fresh_deps,
    mcp_installed,
    open_sink,
    record_schema_read,
)
from adpilot.core.tools import AgentDeps

MIN_KEY_LEN = 24
log = logging.getLogger(__name__)


class AskRequest(BaseModel):
    # extra="forbid" so a `connector` field is rejected rather than ignored: the data source a deployment
    # reads is server-side configuration, never something a caller can repoint.
    model_config = {"extra": "forbid"}

    question: str = Field(min_length=1, max_length=QUESTION_MAX_CHARS)  # tier 1; sanitize_question enforces 600 inside ask()
    session_id: str | None = Field(default=None, max_length=SESSION_ID_MAX_CHARS, pattern=SESSION_ID_PATTERN)


def request_deps(app: FastAPI) -> AgentDeps:
    """A per-request copy of the shared template — see runtime.fresh_deps."""
    return fresh_deps(app.state.deps_template)


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


def key_ok(presented: str | None, expected: str) -> bool:
    # isascii() first: Starlette decodes header bytes as latin-1, and compare_digest raises TypeError
    # (→ 500 traceback for an unauthenticated caller) on a str with any code point above 0x7F.
    return bool(presented) and presented.isascii() and secrets.compare_digest(presented, expected)


class KeyGate:
    """The require_key check for a raw ASGI app. A class, not a function: Starlette treats a plain function
    endpoint as a request handler, not as an ASGI app."""

    def __init__(self, app, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send) -> None:
        if not key_ok(Headers(scope=scope).get("x-api-key"), self.api_key):
            await JSONResponse({"detail": "invalid api key"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(pack: str = "ads", connector: str | None = None) -> FastAPI:
    # The process's logging is the app's, not a dependency's: MCPServer() calls logging.basicConfig(INFO,
    # RichHandler), which on Cloud Run splits each record across 80-column lines and ships every library's INFO
    # chatter. basicConfig is a no-op once the root logger has a handler, so claiming it first keeps it ours.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    configure_tracing()
    api_key = _api_key()
    sink = open_sink(sys.stderr)
    if sink is None:
        raise RuntimeError("audit store unreachable — refusing to serve (ADPILOT_AUDIT=memory to run unrecorded)")

    mcp_server = None  # assigned below, before the lifespan ever runs

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            async with (mcp_server.session_manager.run() if mcp_server else contextlib.nullcontext()):
                yield
        finally:
            # Cloud Run runs with --min-instances 0: a BackgroundTasks flush is not guaranteed to get CPU after the
            # response, so the last rows would die with the instance — and with them if the MCP session manager
            # fails to stop, hence the finally. flush_audit logs whatever it cannot persist.
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

    if mcp_installed():
        from adpilot.mcp_server import build_mcp, http_endpoint

        # One rate bucket for /ask, /ask/stream and /mcp: a remote MCP client must not be a way around the limit.
        # Looked up per call, so replacing app.state.limiter (as the tests do) applies to /mcp too.
        mcp_server = build_mcp(
            app.state.agent, lambda: request_deps(app), sink, limit=lambda: app.state.limiter.try_acquire()
        )
        # POST only. In stateless mode the SDK still answers GET with a standalone event stream that never closes,
        # which would pin a concurrency slot per connected client; DELETE is a 405 from the SDK anyway. A 405 on GET
        # is how a server says it offers no such stream.
        app.add_route("/mcp", KeyGate(http_endpoint(mcp_server), api_key), methods=["POST"])
        app.state.mcp_server = mcp_server
    else:
        log.warning("mcp extra not installed — /mcp is not served (pip install 'adpilot[mcp]')")

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if not key_ok(x_api_key, api_key):
            raise HTTPException(status_code=401, detail="invalid api key")

    def shed() -> None:
        """429 before any work: a shed request never reaches ask() or the audit trail."""
        wait = app.state.limiter.try_acquire()
        if wait:
            raise HTTPException(
                status_code=429, detail="rate limit exceeded",
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )

    @app.get("/healthz")
    def healthz() -> dict:
        # Unauthenticated: it says the process is up and nothing else — not the pack, connector or model chain.
        return {"status": "ok"}

    @app.get("/schema", response_class=PlainTextResponse, dependencies=[Depends(require_key)])
    def get_schema(background: BackgroundTasks) -> str:
        shed()
        record_schema_read(sink, app.state.deps_template.pack.name, "api")
        background.add_task(flush_audit, sink)
        return app.state.deps_template.schema_text

    @app.post("/ask", response_model=AnswerBody, dependencies=[Depends(require_key)])
    def post_ask(req: AskRequest, background: BackgroundTasks) -> AnswerBody:
        shed()
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
        question: str = Query(min_length=1, max_length=QUESTION_MAX_CHARS),
        session_id: str | None = Query(default=None, max_length=SESSION_ID_MAX_CHARS, pattern=SESSION_ID_PATTERN),
    ) -> StreamingResponse:
        shed()
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
            # await above — and it does not need to be: run() executes in an OS thread (anyio.to_thread.run_sync), and
            # cancelling an await cannot stop a running thread, so ask()'s buffered record still reaches
            # flush_audit() in run()'s finally. Pinned by test_a_client_disconnect_still_records_the_run.
            await task

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
