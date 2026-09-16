import io

import pytest

from adpilot.cli import main


@pytest.fixture
def no_api_key(monkeypatch):
    # Empty strings survive load_dotenv (it never overrides existing vars) and read as "unset".
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("LLM_BEARER_TOKEN", "")


def test_schema_command():
    out = io.StringIO()
    assert main(["--connector", "duckdb", "schema"], out=out) == 0
    assert "fct_unified_marketing_performance  (gold)" in out.getvalue()


def test_chat_without_api_key_uses_fallback(no_api_key):
    out = io.StringIO()
    assert main(["--connector", "duckdb", "chat", "-q", "What was spend by platform?"], out=out) == 0
    text = out.getvalue()
    assert "pre-defined queries only" in text
    assert "TikTok" in text and "74266.7" in text
    assert "ModelUnavailable" in text
