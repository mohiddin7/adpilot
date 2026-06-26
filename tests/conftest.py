import os

import pytest
from pydantic_ai import models

from adpilot.connectors import get_connector
from adpilot.core import schema
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
    return AgentDeps(connector=duck, pack=pack, schema_text=schema.summary(duck, pack))
