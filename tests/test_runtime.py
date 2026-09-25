"""runtime.py is the one place a surface turns config into deps; the CLI and the API must agree."""

from adpilot.core.agent import AnalystAnswer
from adpilot.core.audit import MemorySink
from adpilot.core.runtime import AnswerBody, answer_body, build_deps


def test_build_deps_prefers_the_explicit_connector(monkeypatch):
    monkeypatch.setenv("ADPILOT_CONNECTOR", "bigquery")
    deps = build_deps("ads", "duckdb", MemorySink())
    assert deps.connector.dialect == "duckdb"
    assert "fct_unified_marketing_performance" in deps.schema_text


def test_build_deps_falls_back_to_the_env_then_the_pack(monkeypatch):
    monkeypatch.setenv("ADPILOT_CONNECTOR", "duckdb")
    assert build_deps("ads", None, MemorySink()).connector.dialect == "duckdb"
    monkeypatch.delenv("ADPILOT_CONNECTOR")
    assert build_deps("ads", None, MemorySink()).connector.dialect == "duckdb"  # pack default


def test_answer_body_carries_the_trace_id_and_flags_a_refusal():
    body = answer_body(AnalystAnswer(answer_md="no", confidence=1.0, caveats=["OutOfScope"]), "t1")
    assert isinstance(body, AnswerBody)
    assert body.trace_id == "t1" and body.refused is True


def test_answer_body_does_not_flag_a_normal_answer():
    body = answer_body(AnalystAnswer(answer_md="spend was 12", sql="SELECT 1", confidence=0.9), "t2")
    assert body.refused is False and body.sql == "SELECT 1" and body.caveats == []


def test_fresh_deps_shares_the_source_and_resets_per_turn_state(deps):
    from adpilot.core.audit import RunContextInfo
    from adpilot.core.runtime import fresh_deps

    deps.results.append({"rows": 1})
    deps.run_context = RunContextInfo(source="api")
    a, b = fresh_deps(deps), fresh_deps(deps)
    assert a.connector is deps.connector and a.pack is deps.pack and a.audit is deps.audit
    assert a.schema_text == deps.schema_text
    assert a.results == [] and a.results is not b.results and a.budget is not b.budget
    assert a.run_context is None and a.last_result is None


def test_flush_audit_logs_a_partial_flush(caplog):
    from adpilot.core.audit import FlushReport
    from adpilot.core.runtime import flush_audit

    class Partial:
        def flush(self):
            return FlushReport(written={}, failed={"agent_calls": 2}, errors=["503 backend"])

    assert flush_audit(Partial()) is False
    assert "2 row(s) not persisted" in caplog.text and "503 backend" in caplog.text


def test_flush_audit_never_raises(caplog):
    from adpilot.core.runtime import flush_audit

    class Broken:
        def flush(self):
            raise RuntimeError("bigquery is on fire")

    assert flush_audit(Broken()) is False  # must not raise
    assert "flush raised" in caplog.text


def test_mcp_installed_reflects_the_import_system(monkeypatch):
    import importlib.util

    from adpilot.core import runtime

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert runtime.mcp_installed() is False


def test_flush_audit_reports_a_clean_flush():
    from adpilot.core.runtime import flush_audit

    assert flush_audit(MemorySink()) is True
