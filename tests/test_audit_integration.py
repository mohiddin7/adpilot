"""Opt-in: writes and reads one row in a throwaway dataset. Skipped unless ADPILOT_AUDIT_INTEGRATION=1 and creds exist."""

import os
from datetime import UTC, datetime

import pytest

from adpilot.core.audit import AuditRecord, audit_config
from adpilot.core.audit_bigquery import BigQuerySink

pytestmark = pytest.mark.skipif(
    os.environ.get("ADPILOT_AUDIT_INTEGRATION") != "1" or not (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GCP_SERVICE_ACCOUNT_JSON")),
    reason="set ADPILOT_AUDIT_INTEGRATION=1 with GCP credentials to run",
)


def test_preflight_write_and_read_back():
    cfg = audit_config({**os.environ, "BQ_AUDIT_DATASET": "adpilot_audit_test"})
    sink = BigQuerySink(cfg)
    sink.preflight()
    tid = f"it_{datetime.now(UTC):%Y%m%d%H%M%S}"
    sink.record(AuditRecord(trace_id=tid, ts=datetime.now(UTC), environment="local", source="chat", session_id="it", pack="ads", question="q", answer_md="a", messages_json='[{"kind":"request","parts":[{"part_kind":"user-prompt","content":"q"}]}]'))
    rep = sink.flush()
    assert rep.ok, rep.errors
    assert sink.load_session("it", turns=1)
