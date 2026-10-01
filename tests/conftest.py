import os
import sys
from pathlib import Path

import pytest
from pydantic_ai import models

from adpilot.connectors import get_connector
from adpilot.core import schema
from adpilot.core.audit import MemorySink
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import load_pack

APP_DIR = Path(__file__).resolve().parents[1] / "streamlit_app"
sys.path.insert(0, str(APP_DIR))  # the dashboard's `lib` package, as Streamlit itself puts it on the path

os.environ["PYDANTIC_AI_NO_BANNER"] = "1"
models.ALLOW_MODEL_REQUESTS = False
# Layer 1 calls the classifier over plain HTTP, which ALLOW_MODEL_REQUESTS does not cover. `adpilot.cli.main`
# calls load_dotenv(), so a CLI test pulls the developer's .env into the session env for every test after it.
# Empty strings survive load_dotenv (it never overrides an existing var) and read as "unset", so the suite
# cannot reach the classifier whatever .env or the shell says. Tests opt in by monkeypatching `_jev_choice`.
os.environ["ADPILOT_INPUT_CLASSIFIER"] = ""
os.environ["JEV_API_KEY"] = ""
os.environ["LOGFIRE_TOKEN"] = ""  # create_app() calls configure_tracing(); the suite must not ship traces
os.environ["AGENT_LLM_MODELS"] = ""  # empty = the code defaults, whatever chain the developer's .env pins
os.environ["JUDGE_LLM_MODELS"] = ""


@pytest.fixture(scope="session")
def pack():
    return load_pack("ads")


@pytest.fixture(scope="session")
def duck(pack):
    return get_connector("duckdb", pack)


@pytest.fixture
def deps(pack, duck):
    return AgentDeps(connector=duck, pack=pack, schema_text=schema.summary(duck, pack), audit=MemorySink())


@pytest.fixture(scope="session")
def eval_duck(pack):
    from evals.task import load_fixtures

    con = get_connector("duckdb", pack)
    load_fixtures(con, pack)
    return con


@pytest.fixture
def eval_deps_factory(pack, eval_duck):
    from evals.task import RecordingSource

    text = schema.summary(eval_duck, pack)
    return lambda: AgentDeps(connector=RecordingSource(eval_duck), pack=pack, schema_text=text, audit=MemorySink())


@pytest.fixture
def eval_deps(eval_deps_factory):
    return eval_deps_factory()


@pytest.fixture
def no_waits(monkeypatch):
    """Chains from build_chain() rate-limit and back off for real; tests must not sleep. Returns the backoff sleeps."""
    from adpilot.core import guardrails
    from adpilot.core import models as model_chain

    sleeps: list[float] = []
    monkeypatch.setattr(guardrails.MODEL_RATE_LIMITER, "acquire", lambda: None)
    monkeypatch.setattr(model_chain, "_sleep", sleeps.append)
    return sleeps


API_KEY = "k" * 32  # tests/test_api.py and tests/test_mcp.py use the same literal as their KEY


@pytest.fixture
def api(monkeypatch):
    """A started app with a memory sink, no model and DuckDB. Returns (TestClient, MemorySink)."""
    from fastapi.testclient import TestClient

    import adpilot.core.runtime as runtime
    from adpilot.api.app import create_app

    sink = MemorySink()
    monkeypatch.setenv("ADPILOT_API_KEY", API_KEY)
    monkeypatch.setenv("ADPILOT_AUDIT", "memory")
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "")  # no model: ask() takes the rule-based path
    monkeypatch.setattr(runtime, "build_sink", lambda cfg: sink)
    app = create_app(connector="duckdb")
    return TestClient(app), sink


class _Resp:
    """The slice of requests.Response that lib.api_client uses, over a Starlette TestClient response."""

    def __init__(self, r) -> None:
        self._r, self.status_code, self.headers = r, r.status_code, r.headers

    def json(self):
        return self._r.json()

    def iter_lines(self, decode_unicode: bool = False):
        return self._r.iter_lines()

    def close(self) -> None:
        pass


class InProcessSession:
    """Stands in for requests.Session in lib.api_client: the dashboard's client meets the real FastAPI app through
    Starlette's TestClient — same routes, auth and validation, no socket, no network."""

    def __init__(self, client) -> None:
        self.client = client

    def request(self, method, url, headers=None, timeout=None, params=None, json=None, stream=False):
        return _Resp(self.client.request(method, url, headers=headers, params=params, json=json))


@pytest.fixture
def dash_api(api, monkeypatch):
    """lib.api_client wired to the `api` app (DuckDB, no model). Returns (api_client module, TestClient, sink)."""
    import streamlit as st
    from lib import api_client, config

    client, sink = api
    monkeypatch.setattr(api_client, "session", InProcessSession(client))
    monkeypatch.setattr(api_client, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(config, "api_url", lambda: "http://testserver")
    monkeypatch.setattr(config, "api_key", lambda: API_KEY)
    st.cache_data.clear()  # page tests must not see another test's cached reads
    return api_client, client, sink
