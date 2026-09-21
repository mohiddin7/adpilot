"""Opt-in: writes and reads back one row in the real audit dataset. Skipped unless ADPILOT_AUDIT_INTEGRATION=1
and credentials exist. Needs only a dataset-scoped WRITER grant — it never creates a dataset."""

import os
from datetime import UTC, datetime

import pytest
from dotenv import dotenv_values, find_dotenv
from google.cloud import bigquery

from adpilot.core.audit import AuditRecord, audit_config
from adpilot.core.audit_bigquery import BigQuerySink

# Credentials live in .env. Read it without touching os.environ, so importing this module can never
# hand BigQuery credentials to another test.
ENV = {**dotenv_values(find_dotenv(usecwd=True)), **os.environ}

pytestmark = pytest.mark.skipif(
    ENV.get("ADPILOT_AUDIT_INTEGRATION") != "1"
    or not (ENV.get("GOOGLE_APPLICATION_CREDENTIALS") or ENV.get("GCP_SERVICE_ACCOUNT_JSON")),
    reason="set ADPILOT_AUDIT_INTEGRATION=1 with GCP credentials to run",
)


def test_preflight_write_and_read_back():
    cfg = audit_config(ENV)
    sink = BigQuerySink(cfg)
    sink.preflight()
    probe = f"it_{datetime.now(UTC):%Y%m%d%H%M%S%f}"
    try:
        sink.record(
            AuditRecord(
                trace_id=probe, ts=datetime.now(UTC), environment="local", source="chat", session_id=probe,
                pack="ads", question="q", answer_md="a",
                messages_json='[{"kind":"request","parts":[{"part_kind":"user-prompt","content":"q"}]}]',
            )
        )
        rep = sink.flush()
        assert rep.ok, rep.errors
        assert sink.load_session(probe, turns=1)
    finally:
        sink.client.query(
            f"DELETE FROM `{cfg.project}.{cfg.dataset}.agent_calls` WHERE trace_id = @probe",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("probe", "STRING", probe)]
            ),
        ).result()
