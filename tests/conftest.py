import os

import pytest
from pydantic_ai import models

from adpilot.connectors import get_connector
from adpilot.core import schema
from adpilot.core.audit import MemorySink
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import load_pack

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
