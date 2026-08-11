"""HTTP surface tests: TestClient + MemorySink + DuckDB. No model calls, no network, no credentials."""

import pytest

from adpilot.core.audit import MemorySink

KEY = "k" * 32


@pytest.fixture
def api(monkeypatch):
    """A started app with a memory sink, a scripted-free agent and DuckDB. Returns (TestClient, MemorySink)."""
    from fastapi.testclient import TestClient

    import adpilot.core.runtime as runtime
    from adpilot.api.app import create_app

    sink = MemorySink()
    monkeypatch.setenv("ADPILOT_API_KEY", KEY)
    monkeypatch.setenv("ADPILOT_AUDIT", "memory")
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "")  # no model: ask() takes the rule-based path
    monkeypatch.setattr(runtime, "build_sink", lambda cfg: sink)
    app = create_app(connector="duckdb")
    return TestClient(app), sink


def test_healthz_needs_no_key_and_leaks_nothing(api):
    client, _ = api
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_schema_requires_the_key(api):
    client, _ = api
    assert client.get("/schema").status_code == 401
    r = client.get("/schema", headers={"X-API-Key": KEY})
    assert r.status_code == 200 and "fct_unified_marketing_performance  (gold)" in r.text


def test_a_wrong_key_is_rejected_and_writes_no_audit_row(api):
    client, sink = api
    assert client.get("/schema", headers={"X-API-Key": "k" * 31 + "x"}).status_code == 401
    assert sink.calls == []


def test_refuses_to_start_without_a_key(monkeypatch):
    from adpilot.api.app import create_app

    monkeypatch.setenv("ADPILOT_API_KEY", "")
    monkeypatch.setenv("ADPILOT_AUDIT", "memory")
    with pytest.raises(RuntimeError, match="ADPILOT_API_KEY"):
        create_app(connector="duckdb")


def test_refuses_to_start_with_a_short_key(monkeypatch):
    from adpilot.api.app import create_app

    monkeypatch.setenv("ADPILOT_API_KEY", "short-but-not-24-chars")
    monkeypatch.setenv("ADPILOT_AUDIT", "memory")
    with pytest.raises(RuntimeError, match="at least 24"):
        create_app(connector="duckdb")


def test_refuses_to_start_when_the_audit_store_is_unreachable(monkeypatch):
    import adpilot.core.runtime as runtime
    from adpilot.api.app import create_app
    from adpilot.core.audit import AuditUnavailable

    class Broken:
        def preflight(self):
            raise AuditUnavailable("permissions", "grant roles/bigquery.jobUser")

    monkeypatch.setenv("ADPILOT_API_KEY", KEY)
    monkeypatch.delenv("ADPILOT_AUDIT", raising=False)
    monkeypatch.setattr(runtime, "build_sink", lambda cfg: Broken())
    with pytest.raises(RuntimeError, match="audit"):
        create_app(connector="duckdb")
