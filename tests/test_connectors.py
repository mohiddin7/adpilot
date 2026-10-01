import concurrent.futures
import time

import pandas as pd
import pytest

from adpilot.connectors.bigquery import _map_bq_error
from adpilot.connectors.duckdb import DuckDBSource
from adpilot.core.errors import AdPilotError

INIT_SQL = """
CREATE MACRO SAFE_DIVIDE(a, b) AS a / NULLIF(b, 0);
CREATE VIEW t AS SELECT * FROM read_csv_auto('{csv_dir}/t.csv');
"""


@pytest.fixture
def source(tmp_path):
    (tmp_path / "t.csv").write_text("platform,spend\nA,1.5\nB,2.5\n")
    return DuckDBSource(csv_dir=tmp_path, init_sql=INIT_SQL)


def test_query_returns_dataframe(source):
    df = source.query("SELECT platform, SAFE_DIVIDE(spend, 0) AS z FROM t ORDER BY platform")
    assert isinstance(df, pd.DataFrame)
    assert list(df["platform"]) == ["A", "B"]
    assert df["z"].isna().all()


def test_schema_error_has_hint(source):
    with pytest.raises(AdPilotError) as exc:
        source.query("SELECT nope FROM t")
    assert exc.value.kind == "SqlSchema"
    assert "spend" in exc.value.hint


def test_syntax_error(source):
    with pytest.raises(AdPilotError) as exc:
        source.query("SELEC 1")
    assert exc.value.kind == "SqlSyntax"


def test_missing_table(source):
    with pytest.raises(AdPilotError) as exc:
        source.query("SELECT 1 FROM nothere")
    assert exc.value.kind == "SqlSchema"


def test_list_tables_and_columns(source):
    assert source.list_tables() == ["t"]
    assert source.columns("t") == [("platform", "VARCHAR"), ("spend", "DOUBLE")]


def test_no_file_is_readable_after_init(source, tmp_path):
    """Defence in depth behind validate_sql: straight at the connector, no query reads a file or re-opens access."""
    (tmp_path / "other.csv").write_text("secret\n42\n")
    path = (tmp_path / "other.csv").as_posix()
    for sql in [f"SELECT * FROM '{path}'", f"SELECT * FROM read_csv('{path}')", f"SELECT * FROM read_text('{path}')",
                "SET enable_external_access = true", "SET lock_configuration = false"]:
        with pytest.raises(AdPilotError):
            source.query(sql)
    assert list(source.query("SELECT platform FROM t ORDER BY 1")["platform"]) == ["A", "B"]  # loaded at init


def test_bq_error_mapping():
    from google.api_core import exceptions as gexc

    assert _map_bq_error(gexc.BadRequest("Unrecognized name: foo at [1:8]")).kind == "SqlSchema"
    assert _map_bq_error(gexc.BadRequest("Syntax error: Unexpected keyword")).kind == "SqlSyntax"
    assert _map_bq_error(gexc.NotFound("Not found: Table x")).kind == "SqlSchema"
    assert _map_bq_error(gexc.ServiceUnavailable("down")).kind == "DataSourceUnavailable"
    assert _map_bq_error(RuntimeError("creds")).kind == "DataSourceUnavailable"


# ---------- query timeout ----------

HEAVY = ("SELECT COUNT(*) AS n FROM fct_unified_marketing_performance a, fct_unified_marketing_performance b, "
         "fct_unified_marketing_performance c, fct_unified_marketing_performance d")  # 330^4 rows


def _duck(pack, timeout_s):
    cfg = pack.raw["duckdb"]
    return DuckDBSource(csv_dir=pack.repo_root / cfg["csv_dir"], init_sql=(pack.root / cfg["init_sql"]).read_text(),
                        timeout_s=timeout_s)


def test_a_heavy_duckdb_query_is_stopped_at_the_deadline(pack):
    con = _duck(pack, 0.3)
    t0 = time.monotonic()
    with pytest.raises(AdPilotError) as err:
        con.query(HEAVY)
    assert err.value.kind == "QueryTimeout" and time.monotonic() - t0 < 5
    assert err.value.hint  # the model gets something to act on
    assert con.query("SELECT 42 AS x")["x"].tolist() == [42]  # the connection is still usable


def test_a_fast_query_is_untouched_and_the_watchdog_never_fires_later(pack):
    con = _duck(pack, 0.3)
    assert con.query("SELECT 1 AS x")["x"].tolist() == [1]
    time.sleep(0.5)  # past the first query's deadline: a leftover watchdog would interrupt the next query
    assert con.query("SELECT 2 AS x")["x"].tolist() == [2]


def test_a_timeout_releases_the_shared_lock(pack, deps):
    import dataclasses

    from adpilot.core.runtime import fresh_deps
    from adpilot.core.tools import SqlError, execute

    slow = dataclasses.replace(deps, connector=_duck(pack, 0.3))
    res = execute(slow, deps.pack.render("SELECT COUNT(*) AS n FROM {gold} a, {gold} b, {gold} c, {gold} d", "duckdb"))
    assert isinstance(res, SqlError) and res.kind == "QueryTimeout"
    assert execute(fresh_deps(slow), deps.pack.render("SELECT COUNT(*) AS n FROM {gold}", "duckdb")).rows


class _Job:
    def __init__(self, exc=None, cancel_error=None):
        self.exc, self.cancelled, self.cancel_error = exc, False, cancel_error

    def result(self, timeout=None, retry=None):
        self.timeout, self.retry = timeout, retry
        if self.exc:
            raise self.exc
        return self  # stands in for the RowIterator `result()` really returns; `to_dataframe` lives on both

    def cancel(self):
        self.cancelled = True
        if self.cancel_error:
            raise self.cancel_error

    def to_dataframe(self):
        return pd.DataFrame({"x": [1]})


class _Client:
    def __init__(self, job):
        self.job, self.config, self.timeout, self.retry = job, None, None, None

    def query(self, sql, job_config=None, timeout=None, retry=None):
        self.config, self.timeout, self.retry = job_config, timeout, retry
        return self.job


def _bq(job, timeout_s=30):
    from adpilot.connectors.bigquery import BigQuerySource

    src = BigQuerySource("p", default_max_bytes=10, timeout_s=timeout_s)
    src._client = _Client(job)
    return src


def test_bigquery_jobs_carry_the_timeout_both_ways():
    job = _Job()
    src = _bq(job, 30)
    assert src.query("SELECT 1")["x"].tolist() == [1]
    # QueryJobConfig.job_timeout_ms getter returns the stored string, not an int (job/base.py: "docs indicate
    # a string is expected by the API") — confirmed against the installed 3.45.1, not assumed.
    assert int(src._client.config.job_timeout_ms) == 30_000
    # Both the job-start RPC and the poll-for-result RPC are bounded, not just the outer client-side wait:
    # DEFAULT_RETRY's own deadline is 10 minutes, so an unbounded `retry` would let a stalled HTTP call hold
    # the shared lock long past our deadline (google-cloud-bigquery 3.45.1's own client.query/QueryJob.result).
    assert src._client.timeout == 30 and src._client.retry._timeout == 30
    assert job.timeout == 35 and job.retry._timeout == 30


def test_a_bigquery_client_side_timeout_cancels_the_job():
    job = _Job(concurrent.futures.TimeoutError())
    with pytest.raises(AdPilotError) as err:
        _bq(job).query("SELECT 1")
    assert err.value.kind == "QueryTimeout" and job.cancelled


def test_a_failed_cancel_does_not_hide_the_timeout():
    job = _Job(concurrent.futures.TimeoutError(), cancel_error=RuntimeError("cancel failed"))
    with pytest.raises(AdPilotError) as err:
        _bq(job).query("SELECT 1")
    assert err.value.kind == "QueryTimeout" and job.cancelled


def test_a_bigquery_server_side_timeout_is_a_query_timeout():
    from google.api_core import exceptions as gexc

    # Exception shape per job/base.py (google-cloud-bigquery 3.45.1) and dbt-labs/dbt#14618's captured message — not confirmed against a live job.
    with pytest.raises(AdPilotError) as err:
        _bq(_Job(gexc.GoogleAPICallError("Job execution was cancelled: Job timed out after 30 sec, stopped"))).query("SELECT 1")
    assert err.value.kind == "QueryTimeout"


def test_a_bigquery_outage_is_not_mistaken_for_a_timeout():
    """Negative case for the "timed out" substring match: an unrelated outage must still map through unchanged."""
    from google.api_core import exceptions as gexc

    with pytest.raises(AdPilotError) as err:
        _bq(_Job(gexc.ServiceUnavailable("The service is currently unavailable"))).query("SELECT 1")
    assert err.value.kind == "DataSourceUnavailable"
