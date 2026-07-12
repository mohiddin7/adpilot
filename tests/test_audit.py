"""adpilot.core.audit: records, message summary, MemorySink, config."""

from datetime import UTC, datetime

from pydantic_ai import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from adpilot.core.audit import (
    AuditRecord,
    FlushReport,
    MemorySink,
    RunContextInfo,
    RunRow,
    ScoreRow,
    audit_config,
    environment,
    error_kind_of,
    primary_model_name,
    summarize_messages,
)
from adpilot.core.tools import SqlError, SqlResult


def _rec(**over) -> AuditRecord:
    base = dict(
        trace_id="t1", ts=datetime(2026, 7, 11, 2, 47, 19, tzinfo=UTC), environment="local", source="chat", pack="ads",
        question="q", answer_md="a", refused=False, confidence=1.0, caveats=[], model_requested="m", model_used="m",
        fell_back=False, requests=1, tool_calls=[], repairs=0, tokens_in=10, tokens_out=2, cost_usd=0.0, latency_s=0.5,
        messages_json="[]", attributes={"host": "h"},
    )
    base.update(over)
    return AuditRecord(**base)


def test_summarize_messages_counts_calls_tools_and_repairs():
    msgs = [
        ModelRequest(parts=[UserPromptPart("q")]),
        ModelResponse(parts=[ToolCallPart("run_sql", {"sql": "SELECT 1"})], model_name="primary", provider_response_id="r1"),
        ModelRequest(parts=[ToolReturnPart("run_sql", SqlError(kind="SqlSchema", message="bad"), tool_call_id="x")]),
        ModelResponse(parts=[ToolCallPart("run_sql", {"sql": "SELECT 2"})], model_name="secondary", provider_response_id="r2"),
        ModelRequest(parts=[ToolReturnPart("run_sql", SqlResult(sql="SELECT 2", columns=["x"], rows=[{"x": 2}], row_count=1), tool_call_id="y")]),
        ModelResponse(parts=[ToolCallPart("final_result_AnalystAnswer", {"answer_md": "done"})], model_name="secondary"),
    ]
    s = summarize_messages(msgs)
    assert s.model_calls == 3 and s.tool_calls == ["run_sql", "run_sql"] and s.sql_attempted == ["SELECT 1", "SELECT 2"]
    assert s.repairs == 1 and s.model_used == "secondary" and s.provider_response_ids == ["r1", "r2"]
    assert summarize_messages([]).model_used is None


def test_primary_model_name_unwraps_fallback_and_wrappers():
    from pydantic_ai.models.fallback import FallbackModel
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.wrapper import WrapperModel

    a = WrapperModel(FunctionModel(lambda m, i: None, model_name="a"))
    b = FunctionModel(lambda m, i: None, model_name="b")
    assert primary_model_name(FallbackModel(a, b)) == "a"
    assert primary_model_name("openrouter:x") == "openrouter:x"
    assert primary_model_name(None) is None


def test_error_kind_of():
    assert error_kind_of(["ModelRateLimited: answered without the language model"]) == "ModelRateLimited"
    assert error_kind_of(["OutOfScope", "off topic"]) == "OutOfScope"
    assert error_kind_of([]) is None


def test_record_row_is_bigquery_ready():
    row = _rec(caveats=["a"], attributes={"host": "h", "n": 1}).row()
    assert row["ts"] == "2026-07-11T02:47:19Z" and row["caveats"] == ["a"]
    assert row["attributes"] == '{"host": "h", "n": 1}' and row["schema_version"] == 1 and row["messages_json"] == "[]"


def test_memory_sink_round_trip_and_session_history():
    sink = MemorySink()
    for i in range(5):
        msgs = [ModelRequest(parts=[UserPromptPart(f"q{i}")]), ModelResponse(parts=[TextPart(f"a{i}")])]
        from pydantic_ai import ModelMessagesTypeAdapter

        sink.record(_rec(trace_id=f"t{i}", session_id="s1", messages_json=ModelMessagesTypeAdapter.dump_json(msgs).decode(), ts=datetime(2026, 7, 11, 0, i, tzinfo=UTC)))
    sink.record(_rec(trace_id="other", session_id="s2"))
    sink.record(_rec(trace_id="ev", source="eval", session_id="s1"))  # not chat: excluded from history
    history = sink.load_session("s1", turns=3)
    assert [p.content for m in history for p in m.parts] == ["q2", "a2", "q3", "a3", "q4", "a4"]
    assert sink.load_session("nope") == []
    assert len(sink.pending_calls()) == 7
    rep = sink.flush()
    assert isinstance(rep, FlushReport) and rep.ok and rep.written == {"agent_calls": 7} and sink.pending_calls() == []


def test_memory_sink_runs_and_export():
    sink = MemorySink()
    sink.record(_rec(trace_id="t1", source="eval", run_id="run_1", case_name="c1", family="factual"))
    sink.add_scores([ScoreRow(trace_id="t1", run_id="run_1", name="factual", value=1.0, passed=True, source="code", ts=datetime.now(UTC))])
    sink.add_run(RunRow(run_id="run_1", ts=datetime.now(UTC), environment="local", tier="model", pack="ads", case_count=1, overall=90.0, gate_ok=True, gate_reasons=[], models="{}", scorecard_json="{}", invariants_json="[]", calls_used=1, tokens_in=10, tokens_out=2, cost_usd=0.0, duration_s=1.0))
    sink.flush()
    runs = sink.list_runs()
    assert runs[0]["run_id"] == "run_1" and runs[0]["overall"] == 90.0
    rows = list(sink.export_run("run_1"))
    assert rows[0]["trace_id"] == "t1" and rows[0]["scores"][0]["name"] == "factual"
    assert list(sink.export_run("nope")) == []


def test_audit_config_defaults_and_memory_mode():
    cfg = audit_config({})
    assert cfg.mode == "bigquery" and cfg.project == "adpilot-lakehouse" and cfg.dataset == "adpilot_audit" and cfg.location is None
    cfg = audit_config({"ADPILOT_AUDIT": "memory", "BQ_PROJECT_ID": "p", "BQ_AUDIT_DATASET": "d", "BQ_LOCATION": "EU", "GCP_SERVICE_ACCOUNT_JSON": "{}"})
    assert cfg.mode == "memory" and cfg.project == "p" and cfg.dataset == "d" and cfg.location == "EU" and cfg.service_account_json == "{}"


def test_environment_detection():
    assert environment({}) == "local"
    assert environment({"GITHUB_ACTIONS": "true"}) == "ci"
    assert environment({"STREAMLIT_RUNTIME": "1"}) == "cloud" and environment({"K_SERVICE": "svc"}) == "cloud"


def test_run_context_defaults():
    c = RunContextInfo()
    assert c.source == "chat" and c.run_id is None


def test_pack_prompt_hash(pack):
    from evals.scorecard import prompt_hash

    assert len(pack.prompt_hash) == 12 and prompt_hash(pack) == pack.prompt_hash


def test_build_record_from_usage_and_messages():
    from pydantic_ai.usage import RunUsage

    from adpilot.core.audit import build_record

    msgs = [ModelRequest(parts=[UserPromptPart("q")]), ModelResponse(parts=[TextPart("a")], model_name="secondary")]
    rec = build_record(
        trace_id="t", ts=datetime.now(UTC), latency_s=1.234, question="q", answer_md="a", sql=None, refused=False, confidence=0.9,
        caveats=["ModelRateLimited: x"], messages=msgs, usage=RunUsage(input_tokens=12, output_tokens=3, requests=1),
        model_requested="primary", context=RunContextInfo(source="eval", run_id="r", case_name="c", family="factual"),
        pack_name="ads", prompt_hash="abc",
    )
    assert rec.model_used == "secondary" and rec.fell_back is True and rec.tokens_in == 12 and rec.tokens_out == 3
    assert rec.cost_usd == 0.0 and rec.requests == 1 and rec.error_kind == "ModelRateLimited" and rec.latency_s == 1.234
    assert rec.source == "eval" and rec.run_id == "r" and rec.case_name == "c" and rec.family == "factual"
    assert "host" in rec.attributes and rec.otel_trace_id is None and '"kind":"response"' in rec.messages_json


def test_build_record_without_model_result():
    from adpilot.core.audit import build_record

    rec = build_record(
        trace_id="t", ts=datetime.now(UTC), latency_s=0.0, question="q", answer_md="blocked", sql=None, refused=True, confidence=0.0,
        caveats=["SqlPolicy"], messages=[], usage=None, model_requested="primary", context=RunContextInfo(), pack_name="ads", prompt_hash="abc",
    )
    assert rec.model_used is None and rec.fell_back is False and rec.tokens_in == 0 and rec.messages_json == "[]" and rec.error_kind == "SqlPolicy"
