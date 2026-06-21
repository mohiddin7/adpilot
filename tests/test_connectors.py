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


def test_bq_error_mapping():
    from google.api_core import exceptions as gexc

    assert _map_bq_error(gexc.BadRequest("Unrecognized name: foo at [1:8]")).kind == "SqlSchema"
    assert _map_bq_error(gexc.BadRequest("Syntax error: Unexpected keyword")).kind == "SqlSyntax"
    assert _map_bq_error(gexc.NotFound("Not found: Table x")).kind == "SqlSchema"
    assert _map_bq_error(gexc.ServiceUnavailable("down")).kind == "DataSourceUnavailable"
    assert _map_bq_error(RuntimeError("creds")).kind == "DataSourceUnavailable"
