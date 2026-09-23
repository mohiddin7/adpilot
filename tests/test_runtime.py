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
