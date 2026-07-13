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


def test_chat_without_api_key_uses_fallback(no_api_key, memory_audit):
    out = io.StringIO()
    assert main(["--connector", "duckdb", "chat", "-q", "What was spend by platform?"], out=out) == 0
    text = out.getvalue()
    assert "pre-defined queries only" in text
    assert "TikTok" in text and "74266.7" in text
    assert "ModelUnavailable" in text


def test_eval_subcommand(tmp_path):
    import io

    from adpilot.cli import main

    out = io.StringIO()
    assert main(["eval", "--tier", "deterministic", "--out", str(tmp_path), "--family", "scope"], out=out) == 0
    assert "Overall" in out.getvalue()
    assert main(["eval", "--check-cases"], out=io.StringIO()) == 0


def test_eval_summary_uses_the_baseline_it_gated_against(tmp_path):
    assert main(["eval", "--tier", "deterministic", "--out", str(tmp_path), "--baseline-update"], out=io.StringIO()) == 0
    out = io.StringIO()
    assert main(["eval", "--tier", "deterministic", "--out", str(tmp_path)], out=out) == 0
    assert "(no baseline)" not in out.getvalue()


@pytest.fixture
def memory_audit(monkeypatch):
    monkeypatch.setenv("ADPILOT_AUDIT", "memory")


def test_chat_prints_memory_audit_notice_and_records(no_api_key, memory_audit, monkeypatch):
    import adpilot.cli as cli
    from adpilot.core.audit import MemorySink

    sink = MemorySink()
    monkeypatch.setattr(cli, "build_sink", lambda cfg: sink)
    out = io.StringIO()
    assert main(["--connector", "duckdb", "chat", "-q", "What was spend by platform?", "--session", "s1"], out=out) == 0
    assert "audit: memory" in out.getvalue()
    assert len(sink.calls) == 1 and sink.calls[0].session_id == "s1" and sink.calls[0].source == "chat" and sink.flushed["agent_calls"] == 1


def test_chat_exits_2_when_audit_unavailable(no_api_key, monkeypatch):
    import adpilot.cli as cli
    from adpilot.core.audit import AuditUnavailable

    class Broken:
        def preflight(self):
            raise AuditUnavailable("permissions", "grant roles")

    monkeypatch.delenv("ADPILOT_AUDIT", raising=False)
    monkeypatch.setattr(cli, "build_sink", lambda cfg: Broken())
    out = io.StringIO()
    assert main(["--connector", "duckdb", "chat", "-q", "spend by platform"], out=out) == 2
    assert "audit unavailable (permissions): grant roles" in out.getvalue()


def test_chat_warns_when_turn_not_persisted(no_api_key, memory_audit, monkeypatch):
    import adpilot.cli as cli
    from adpilot.core.audit import FlushReport, MemorySink

    class Flaky(MemorySink):
        def flush(self):
            super().flush()
            return FlushReport(failed={"agent_calls": 1}, errors=["agent_calls: 503"])

    monkeypatch.setattr(cli, "build_sink", lambda cfg: Flaky())
    out = io.StringIO()
    assert main(["--connector", "duckdb", "chat", "-q", "spend by platform"], out=out) == 1
    assert "TikTok" in out.getvalue() and "audit: 1 row(s) not persisted" in out.getvalue()


def test_audit_subcommands_against_memory_sink(memory_audit, monkeypatch):
    from datetime import UTC, datetime

    import adpilot.cli as cli
    from adpilot.core.audit import AuditRecord, MemorySink, RunRow, ScoreRow

    sink = MemorySink()
    sink.record(AuditRecord(trace_id="t1", ts=datetime.now(UTC), environment="local", source="eval", run_id="run_1", pack="ads", question="q", answer_md="a"))
    sink.add_scores([ScoreRow(trace_id="t1", run_id="run_1", name="factual", value=1.0, passed=True, ts=datetime.now(UTC))])
    sink.add_run(RunRow(run_id="run_1", ts=datetime.now(UTC), environment="local", tier="model", pack="ads", overall=88.0, gate_ok=True))
    sink.flush()
    monkeypatch.setattr(cli, "build_sink", lambda cfg: sink)

    out = io.StringIO()
    assert main(["audit", "preflight"], out=out) == 0 and "audit: memory" in out.getvalue()
    out = io.StringIO()
    assert main(["audit", "runs"], out=out) == 0 and "run_1" in out.getvalue() and "88.0" in out.getvalue()
    out = io.StringIO()
    assert main(["audit", "export", "--run", "run_1"], out=out) == 0
    import json

    line = json.loads(out.getvalue().strip().splitlines()[0])
    assert line["trace_id"] == "t1" and line["scores"][0]["name"] == "factual"
    out = io.StringIO()
    assert main(["audit", "export", "--run", "run_1", "--csv"], out=out) == 0 and out.getvalue().startswith("trace_id,")
    out = io.StringIO()
    assert main(["audit", "export", "--run", "nope"], out=out) == 1
