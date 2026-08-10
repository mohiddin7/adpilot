"""BigQuerySink against a fake client: no network, every branch of preflight/flush exercised."""

import json
from datetime import UTC, datetime

import pytest
from google.api_core import exceptions as gex
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import bigquery

from adpilot.core.audit import AuditConfig, AuditRecord, AuditUnavailable, RunRow, ScoreRow
from adpilot.core.audit_bigquery import TABLES, BigQuerySink

CFG = AuditConfig(mode="bigquery", project="adpilot-lakehouse", dataset="adpilot_audit", location=None, service_account_json=None, credentials_path=None)


class FakeJob:
    def __init__(self, fail=None):
        self._fail = fail

    def result(self, timeout=None):
        if self._fail:
            raise self._fail
        return self


class FakeClient:
    """Just enough of google.cloud.bigquery.Client for the sink. `fail_loads` is a queue of exceptions raised by
    successive load jobs (None = success)."""

    def __init__(self, datasets=None, tables=None, fail_loads=None, staging_location="EU", dry_run_error=None):
        self.project = "adpilot-lakehouse"
        self.datasets = dict(datasets or {})  # "proj.ds" -> location
        self.tables = dict(tables or {})  # "proj.ds.table" -> list[SchemaField]
        self.fail_loads = list(fail_loads or [])
        self.staging_location = staging_location
        self.dry_run_error = dry_run_error
        self.loads: list[tuple[str, list[dict]]] = []
        self.created_datasets: list[tuple[str, str]] = []
        self.updated_tables: list[tuple[str, list[str]]] = []

    def get_dataset(self, ref):
        ref = str(ref)
        if ref in self.datasets:
            ds = bigquery.Dataset(ref)
            ds.location = self.datasets[ref]
            return ds
        if not ref.endswith(".adpilot_audit") and self.staging_location is not None:  # the pipeline's staging dataset
            ds = bigquery.Dataset(ref)
            ds.location = self.staging_location
            return ds
        raise gex.NotFound(ref)

    def create_dataset(self, ds, exists_ok=False):
        self.datasets[f"{ds.project}.{ds.dataset_id}"] = ds.location
        self.created_datasets.append((ds.dataset_id, ds.location))
        return ds

    def get_table(self, ref):
        ref = str(ref)
        if ref not in self.tables:
            raise gex.NotFound(ref)
        t = bigquery.Table(ref, schema=self.tables[ref])
        return t

    def create_table(self, table):
        self.tables[str(table.reference)] = list(table.schema)
        return table

    def update_table(self, table, fields):
        self.tables[str(table.reference)] = list(table.schema)
        self.updated_tables.append((table.table_id, [f.name for f in table.schema]))
        return table

    def query(self, sql, job_config=None):
        if self.dry_run_error:
            raise self.dry_run_error
        return FakeJob()

    def load_table_from_file(self, fh, ref, job_config=None):
        rows = [json.loads(line) for line in fh.read().decode().splitlines() if line]
        fail = self.fail_loads.pop(0) if self.fail_loads else None
        if fail is None:
            self.loads.append((str(ref).split(".")[-1], rows))
        return FakeJob(fail)


def _rec(i=0, **over):
    base = dict(trace_id=f"t{i}", ts=datetime(2026, 7, 12, 16, 31, tzinfo=UTC), environment="local", source="chat", pack="ads", question="q", answer_md="a")
    base.update(over)
    return AuditRecord(**base)


def test_preflight_creates_dataset_in_staging_location_and_tables():
    client = FakeClient(staging_location="EU")
    sink = BigQuerySink(CFG, client=client)
    sink.preflight()
    assert client.created_datasets == [("adpilot_audit", "EU")]
    assert set(t.split(".")[-1] for t in client.tables) == {"agent_calls", "scores", "eval_runs"}
    cols = [f.name for f in client.tables["adpilot-lakehouse.adpilot_audit.agent_calls"]]
    assert cols[:3] == ["trace_id", "otel_trace_id", "ts"] and "attributes" in cols and "schema_version" in cols


def test_preflight_uses_explicit_location_and_default_us():
    client = FakeClient(staging_location=None)
    BigQuerySink(AuditConfig(**{**CFG.__dict__, "location": "asia-south1"}), client=client).preflight()
    assert client.created_datasets == [("adpilot_audit", "asia-south1")]
    client = FakeClient(staging_location=None)
    BigQuerySink(CFG, client=client).preflight()
    assert client.created_datasets == [("adpilot_audit", "US")]


def test_preflight_adds_missing_columns_additively():
    existing = [bigquery.SchemaField(n, t, mode=m) for n, t, m in TABLES["scores"].fields if n != "attributes"]
    client = FakeClient(datasets={"adpilot-lakehouse.adpilot_audit": "US"}, tables={"adpilot-lakehouse.adpilot_audit.scores": existing})
    BigQuerySink(CFG, client=client).preflight()
    table_id, cols = client.updated_tables[0]
    assert table_id == "scores" and set(cols) == {n for n, _, _ in TABLES["scores"].fields} and cols[-1] == "attributes"


def test_preflight_refuses_on_type_mismatch():
    fields = [bigquery.SchemaField(n, "STRING" if n == "overall" else t, mode=m) for n, t, m in TABLES["eval_runs"].fields]
    client = FakeClient(datasets={"adpilot-lakehouse.adpilot_audit": "US"}, tables={"adpilot-lakehouse.adpilot_audit.eval_runs": fields})
    with pytest.raises(AuditUnavailable) as e:
        BigQuerySink(CFG, client=client).preflight()
    assert e.value.kind == "schema" and "overall" in e.value.hint


def test_preflight_maps_credentials_and_permission_errors():
    with pytest.raises(AuditUnavailable) as e:
        BigQuerySink(CFG, client=FakeClient(dry_run_error=gex.Forbidden("no jobUser"))).preflight()
    assert e.value.kind == "permissions" and "jobUser" in e.value.hint

    class NoCreds(FakeClient):
        def get_dataset(self, ref):
            raise DefaultCredentialsError("none")

    with pytest.raises(AuditUnavailable) as e:
        BigQuerySink(CFG, client=NoCreds()).preflight()
    assert e.value.kind == "credentials" and "GOOGLE_APPLICATION_CREDENTIALS" in e.value.hint


def test_flush_loads_each_table_and_clears_buffer():
    client = FakeClient()
    sink = BigQuerySink(CFG, client=client)
    sink.record(_rec(1))
    sink.record(_rec(2, source="eval", run_id="r"))
    sink.add_scores([ScoreRow(trace_id="t2", run_id="r", name="factual", value=1.0, passed=True, ts=datetime.now(UTC))])
    sink.add_run(RunRow(run_id="r", ts=datetime.now(UTC), environment="local", tier="model", pack="ads"))
    assert len(sink.pending_calls()) == 2
    rep = sink.flush()
    assert rep.ok and rep.written == {"agent_calls": 2, "scores": 1, "eval_runs": 1}
    assert [t for t, _ in client.loads] == ["agent_calls", "scores", "eval_runs"]
    assert client.loads[0][1][0]["trace_id"] == "t1" and client.loads[0][1][0]["ts"].startswith("2026-07-12T16:31")
    assert sink.pending_calls() == [] and sink.flush().written == {}


def test_flush_retries_transient_then_succeeds():
    sleeps = []
    client = FakeClient(fail_loads=[gex.ServiceUnavailable("503"), None])
    sink = BigQuerySink(CFG, client=client, sleep=sleeps.append)
    sink.record(_rec())
    rep = sink.flush()
    assert rep.ok and sleeps == [1] and len(client.loads) == 1


def test_flush_gives_up_after_three_transient_failures_and_keeps_batch():
    sleeps = []
    client = FakeClient(fail_loads=[gex.ServerError("500"), gex.TooManyRequests("rateLimitExceeded"), ConnectionError("reset")])
    sink = BigQuerySink(CFG, client=client, sleep=sleeps.append)
    sink.record(_rec())
    rep = sink.flush()
    assert not rep.ok and rep.failed == {"agent_calls": 1} and sleeps == [1, 4] and len(sink.pending_calls()) == 1
    assert rep.errors and "agent_calls" in rep.errors[0]
    client.fail_loads = []
    assert sink.flush().ok and sink.pending_calls() == []


def test_flush_stops_at_first_failed_table_and_leaves_the_rest_buffered():
    """agent_calls fails (exhausts retries); scores/eval_runs would themselves load fine, but must not be
    attempted — writing them while their agent_calls rows are stuck would orphan every join in
    docs/observability.md. The whole batch must stay buffered together for the next flush to retry."""
    sleeps = []
    client = FakeClient(fail_loads=[gex.ServerError("500"), gex.ServerError("500"), gex.ServerError("500")])
    sink = BigQuerySink(CFG, client=client, sleep=sleeps.append)
    sink.record(_rec(1, source="eval", run_id="r"))
    sink.add_scores([ScoreRow(trace_id="t1", run_id="r", name="factual", value=1.0, passed=True, ts=datetime.now(UTC))])
    sink.add_run(RunRow(run_id="r", ts=datetime.now(UTC), environment="local", tier="model", pack="ads"))

    rep = sink.flush()

    assert not rep.ok
    assert rep.written == {}
    assert rep.failed == {"agent_calls": 1, "scores": 1, "eval_runs": 1}
    assert client.loads == []  # scores/eval_runs were never even attempted
    assert len(sink.pending_calls()) == 1
    assert len(sink._buffers["scores"]) == 1 and len(sink._buffers["eval_runs"]) == 1

    # once agent_calls can load, a retry flushes everything together in order
    client.fail_loads = []
    rep2 = sink.flush()
    assert rep2.ok and rep2.written == {"agent_calls": 1, "scores": 1, "eval_runs": 1}
    assert [t for t, _ in client.loads] == ["agent_calls", "scores", "eval_runs"]


def test_flush_writes_earlier_tables_and_stops_before_a_later_failure():
    """agent_calls succeeds; scores fails outright (non-transient) — eval_runs must not be attempted
    even though it would itself succeed, since it summarizes calls that scores hasn't been recorded for."""
    client = FakeClient(fail_loads=[None, gex.Forbidden("nope")])
    sink = BigQuerySink(CFG, client=client)
    sink.record(_rec(1))
    sink.add_scores([ScoreRow(trace_id="t1", run_id="r", name="factual", value=1.0, passed=True, ts=datetime.now(UTC))])
    sink.add_run(RunRow(run_id="r", ts=datetime.now(UTC), environment="local", tier="model", pack="ads"))

    rep = sink.flush()

    assert not rep.ok
    assert rep.written == {"agent_calls": 1}
    assert rep.failed == {"scores": 1, "eval_runs": 1}
    assert [t for t, _ in client.loads] == ["agent_calls"]  # eval_runs never attempted
    assert sink.pending_calls() == []  # agent_calls did commit and clear
    assert len(sink._buffers["scores"]) == 1 and len(sink._buffers["eval_runs"]) == 1


def test_flush_does_not_retry_permission_errors():
    sleeps = []
    client = FakeClient(fail_loads=[gex.Forbidden("nope")])
    sink = BigQuerySink(CFG, client=client, sleep=sleeps.append)
    sink.record(_rec())
    rep = sink.flush()
    assert not rep.ok and sleeps == [] and rep.failed == {"agent_calls": 1}


def test_load_session_query_covers_chat_api_and_mcp():
    """The stored query must not silently drop a surface: a session is continuable wherever it started."""
    client = FakeClient()
    captured = {}

    class Rows(FakeJob):
        def result(self, timeout=None):
            return []

    def query(sql, job_config=None):
        captured["sql"] = sql
        return Rows()

    client.query = query
    assert BigQuerySink(CFG, client=client).load_session("s1") == []
    assert "source IN ('chat','api','mcp')" in captured["sql"]
    assert "source = 'chat'" not in captured["sql"]
