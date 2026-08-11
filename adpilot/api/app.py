"""HTTP surface: transport in, ask() in the middle, transport out.

No guardrail, model-fallback or audit logic lives here — all of it is inside ask(). A rejected key never
reaches ask(), so a 401 writes no audit row; a refused *question* does reach it, so it returns 200 and is
recorded, exactly as the CLI shows it.
"""

from __future__ import annotations

import os
import secrets
import sys

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse

from adpilot.core.agent import build_agent
from adpilot.core.models import build_model
from adpilot.core.runtime import build_deps, configure_tracing, open_sink

MIN_KEY_LEN = 24


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

    return app
