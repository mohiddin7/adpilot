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
