"""MCP surface: two tools over the same ask() every other surface calls.

`ask` and `schema`, nothing else. The agent's own tools (run_sql, get_anomalies, get_forecast, get_budget_plan)
are deliberately not re-exposed: that would make the MCP client the analyst and open a second path to the data
source beside the guarded one. Every question goes through ask(), so the guard chain, the model-fallback chain,
output redaction and the audit record are the ones the CLI and the HTTP API get.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from adpilot.core.agent import ask
from adpilot.core.audit import AuditSink, RunContextInfo
from adpilot.core.runtime import (
    QUESTION_MAX_CHARS,
    SESSION_ID_MAX_CHARS,
    SESSION_ID_PATTERN,
    AnswerBody,
    answer_body,
    flush_audit,
    record_schema_read,
)
from adpilot.core.tools import AgentDeps

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "AdPilot is an AI analyst over marketing performance data. Use `ask` for any question about spend, "
    "conversions, CPA, ROAS, anomalies, forecasts or budget reallocation; it writes and runs the SQL itself and "
    "returns the answer with the SQL, rows and caveats. Use `schema` to see which tables and columns exist. "
    "A `refused: true` answer is a deliberate refusal (out of scope or blocked input), not a failure."
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def build_mcp(
    agent,
    deps_factory: Callable[[], AgentDeps],
    sink: AuditSink,
    limit: Callable[[], float] | None = None,
) -> MCPServer:
    """The server object both transports serve. `limit()` returns seconds to wait, 0.0 to go (try_acquire)."""
    server = MCPServer("adpilot", instructions=INSTRUCTIONS)

    def shed() -> None:
        # Before any work: like the API's 429, a shed call is not a surface call and writes no audit row.
        if limit is not None and (wait := limit()):
            raise ToolError(f"rate limit exceeded — retry in {max(1, math.ceil(wait))} s")

    # Sync tools run in the SDK's worker thread (anyio.to_thread), which is what the blocking ask() needs.
    @server.tool(name="ask", annotations=READ_ONLY)
    def ask_question(
        question: Annotated[str, Field(min_length=1, max_length=QUESTION_MAX_CHARS)],
        session_id: Annotated[
            str | None, Field(max_length=SESSION_ID_MAX_CHARS, pattern=SESSION_ID_PATTERN)
        ] = None,
    ) -> AnswerBody:
        """Answer a question about the marketing data in plain English.

        Returns the answer as markdown plus the SQL that produced it, the result rows, an optional chart spec,
        a confidence score, caveats and whether the question was refused. Pass the same session_id on follow-up
        questions to keep the last three turns as context.
        """
        shed()
        try:
            deps = deps_factory()
            deps.run_context = RunContextInfo(source="mcp", session_id=session_id)
            history = sink.load_session(session_id) if session_id else None
            answer, _messages, trace_id = ask(agent, deps, question, history=history)
        except Exception:
            # ask() maps its own failures to answers; anything reaching here is a sink or wiring fault (a BigQuery
            # load_session error, say). The SDK would hand str(exc) — project and table names — to the caller.
            log.exception("mcp: ask failed outside ask()'s own error handling")
            raise ToolError("internal error — the server log has the details") from None
        finally:
            flush_audit(sink)
        return answer_body(answer, trace_id)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def schema() -> str:
        """The tables and columns `ask` can query, with the data pack's column descriptions."""
        shed()
        deps = deps_factory()
        try:
            record_schema_read(sink, deps.pack.name, "mcp")
        finally:
            flush_audit(sink)
        return deps.schema_text

    return server


def http_endpoint(server: MCPServer):
    """The streamable-HTTP handler, for registering on an existing app at one exact path.

    The caller must run `server.session_manager.run()` in its lifespan (it can run once per server object).
    - Stateless, JSON responses: each POST stands alone, so any Cloud Run instance can serve any call — a stateful
      session would be pinned to the instance that minted it. The SDK would still hold a GET event stream open, so
      the route is registered for POST only (see adpilot/api/app.py).
    - DNS-rebinding protection off: the SDK's version is a localhost Host allowlist that would reject the deployed
      *.run.app host. What it defends against, a hostile page reaching a local server through the user's browser,
      is covered by X-API-Key, which that page does not have.
    """
    # streamable_http_app() is called for its side effect: it builds server.session_manager with these settings.
    # Its Starlette app is not used — mounting it would make /mcp a redirect to /mcp/.
    server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return StreamableHTTPASGIApp(server.session_manager)
