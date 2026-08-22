import io

import pytest

from adpilot.cli import main


@pytest.fixture
def no_api_key(monkeypatch):
    # Empty strings survive load_dotenv (it never overrides existing vars) and read as "unset".
    monkeypatch.setenv("AGENT_LLM_BEARER_TOKEN", "")
    for legacy in ("OPENROUTER_API_KEY", "LLM_BEARER_TOKEN"):
        monkeypatch.delenv(legacy, raising=False)


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


def test_eval_prints_memory_audit_notice_before_running(tmp_path):
    """Deterministic-tier eval always uses a MemorySink (evals/run.py never touches ADPILOT_AUDIT for it),
    so it must always say so up front — the same guarantee chat/audit already give for memory mode."""
    out = io.StringIO()
    assert main(["eval", "--tier", "deterministic", "--out", str(tmp_path), "--family", "scope"], out=out) == 0
    text = out.getvalue()
    assert text.startswith("audit: memory")
    assert "Overall" in text


def test_eval_check_cases_prints_no_audit_notice():
    out = io.StringIO()
    assert main(["eval", "--check-cases"], out=out) == 0
    assert "audit:" not in out.getvalue()


def test_eval_summary_uses_the_baseline_it_gated_against(tmp_path):
    assert main(["eval", "--tier", "deterministic", "--out", str(tmp_path), "--baseline-update"], out=io.StringIO()) == 0
    out = io.StringIO()
    assert main(["eval", "--tier", "deterministic", "--out", str(tmp_path)], out=out) == 0
    assert "(no baseline)" not in out.getvalue()


@pytest.fixture
def memory_audit(monkeypatch):
    monkeypatch.setenv("ADPILOT_AUDIT", "memory")


def test_chat_prints_memory_audit_notice_and_records(no_api_key, memory_audit, monkeypatch):
    from adpilot.core.audit import MemorySink

    sink = MemorySink()
    import adpilot.core.runtime as runtime

    monkeypatch.setattr(runtime, "build_sink", lambda cfg: sink)
    out = io.StringIO()
    assert main(["--connector", "duckdb", "chat", "-q", "What was spend by platform?", "--session", "s1"], out=out) == 0
    assert "audit: memory" in out.getvalue()
    assert len(sink.calls) == 1 and sink.calls[0].session_id == "s1" and sink.calls[0].source == "chat" and sink.flushed["agent_calls"] == 1


def test_chat_exits_2_when_audit_unavailable(no_api_key, monkeypatch):
    from adpilot.core.audit import AuditUnavailable

    class Broken:
        def preflight(self):
            raise AuditUnavailable("permissions", "grant roles")

    monkeypatch.delenv("ADPILOT_AUDIT", raising=False)
    import adpilot.core.runtime as runtime

    monkeypatch.setattr(runtime, "build_sink", lambda cfg: Broken())
    out = io.StringIO()
    assert main(["--connector", "duckdb", "chat", "-q", "spend by platform"], out=out) == 2
    assert "audit unavailable (permissions): grant roles" in out.getvalue()


def test_chat_warns_when_turn_not_persisted(no_api_key, memory_audit, monkeypatch):
    from adpilot.core.audit import FlushReport, MemorySink

    class Flaky(MemorySink):
        def flush(self):
            super().flush()
            return FlushReport(failed={"agent_calls": 1}, errors=["agent_calls: 503"])

    import adpilot.core.runtime as runtime

    monkeypatch.setattr(runtime, "build_sink", lambda cfg: Flaky())
    out = io.StringIO()
    assert main(["--connector", "duckdb", "chat", "-q", "spend by platform"], out=out) == 1
    assert "TikTok" in out.getvalue() and "audit: 1 row(s) not persisted" in out.getvalue()


def test_chat_flushes_the_turn_even_when_printing_the_answer_raises(no_api_key, memory_audit, monkeypatch):
    """_print builds a DataFrame from model-supplied answer.data and can raise; the record ask() already
    buffered for this turn must still reach the sink instead of dying with the crash."""
    import adpilot.cli as cli
    from adpilot.core.audit import MemorySink

    sink = MemorySink()
    import adpilot.core.runtime as runtime

    monkeypatch.setattr(runtime, "build_sink", lambda cfg: sink)

    def boom(answer, out):
        raise ValueError("bad answer.data")

    monkeypatch.setattr(cli, "_print", boom)
    with pytest.raises(ValueError, match="bad answer.data"):
        main(["--connector", "duckdb", "chat", "-q", "What was spend by platform?"], out=io.StringIO())
    assert len(sink.calls) == 1 and sink.flushed["agent_calls"] == 1


def test_audit_subcommands_against_memory_sink(memory_audit, monkeypatch):
    from datetime import UTC, datetime

    import adpilot.cli as cli
    from adpilot.core.audit import AuditRecord, MemorySink, RunRow, ScoreRow

    sink = MemorySink()
    sink.record(AuditRecord(trace_id="t1", ts=datetime.now(UTC), environment="local", source="eval", run_id="run_1", pack="ads", question="q", answer_md="a"))
    sink.add_scores([ScoreRow(trace_id="t1", run_id="run_1", name="factual", value=1.0, passed=True, ts=datetime.now(UTC))])
    sink.add_run(RunRow(run_id="run_1", ts=datetime.now(UTC), environment="local", tier="model", pack="ads", overall=88.0, gate_ok=True))
    sink.flush()
    import adpilot.core.runtime as runtime

    # cmd_audit's "runs"/"export" paths call build_sink directly (cli's own import); "preflight" goes
    # through open_sink, which resolves build_sink in runtime's namespace. Both need the same sink.
    monkeypatch.setattr(runtime, "build_sink", lambda cfg: sink)
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


def test_brief_without_model_prints_raw_answers_and_exits_1(no_api_key, memory_audit, monkeypatch, capsys):
    import adpilot.core.runtime as runtime
    from adpilot.core.audit import MemorySink

    sink = MemorySink()
    monkeypatch.setattr(runtime, "build_sink", lambda cfg: sink)
    out = io.StringIO()
    assert main(["--connector", "duckdb", "brief"], out=out) == 1
    assert out.getvalue().startswith("# ")  # stdout is markdown only; the audit banner went to stderr
    assert "## Unavailable" in out.getvalue()
    assert "audit: memory" in capsys.readouterr().err
    assert sink.flushed["agent_calls"] == 4


def test_brief_with_model_writes_the_file_and_exits_0(no_api_key, memory_audit, monkeypatch, tmp_path):
    import adpilot.cli as cli
    from tests.test_brief import brief_model

    model, _ = brief_model()
    monkeypatch.setattr(cli, "build_model", lambda: model)
    target = tmp_path / "brief.md"
    assert main(["--connector", "duckdb", "brief", "--out", str(target)], out=io.StringIO()) == 0
    assert "## What changed" in target.read_text()


def test_brief_refuses_to_run_unrecorded(no_api_key, monkeypatch, tmp_path):
    import adpilot.core.runtime as runtime
    from adpilot.core.audit import AuditUnavailable

    class Broken:
        def preflight(self):
            raise AuditUnavailable("permissions", "grant roles")

    monkeypatch.delenv("ADPILOT_AUDIT", raising=False)
    monkeypatch.setattr(runtime, "build_sink", lambda cfg: Broken())
    target = tmp_path / "brief.md"
    assert main(["--connector", "duckdb", "brief", "--out", str(target)], out=io.StringIO()) == 2
    assert not target.exists()


def test_brief_exits_2_when_the_audit_flush_fails(no_api_key, memory_audit, monkeypatch, tmp_path):
    import adpilot.core.runtime as runtime
    from adpilot.core.audit import FlushReport, MemorySink

    class Flaky(MemorySink):
        def flush(self):
            super().flush()
            return FlushReport(failed={"agent_calls": 4}, errors=["agent_calls: 503"])

    monkeypatch.setattr(runtime, "build_sink", lambda cfg: Flaky())
    target = tmp_path / "brief.md"
    assert main(["--connector", "duckdb", "brief", "--out", str(target)], out=io.StringIO()) == 2
    assert target.exists()  # the brief is still written; only the record is at risk


def test_brief_needs_briefing_questions(no_api_key, memory_audit, monkeypatch, capsys):
    import adpilot.core.brief as brief

    monkeypatch.setattr(brief, "questions", lambda pack: [])
    assert main(["--connector", "duckdb", "brief"], out=io.StringIO()) == 2
    assert "defines no briefing questions" in capsys.readouterr().err
