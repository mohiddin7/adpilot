import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402

ingest = common.load_script("01_validate_and_ingest")
RAW = Path(__file__).resolve().parents[2] / "data" / "raw"
CSV = {"facebook": "01_facebook_ads.csv", "google": "02_google_ads.csv", "tiktok": "03_tiktok_ads.csv"}


def _cols(platform):
    return pd.read_csv(RAW / CSV[platform], nrows=0).columns.tolist() + ["ingested_at", "source_file"]


@pytest.mark.parametrize("platform", list(CSV))
def test_merge_updates_every_non_key_column(platform):
    cfg = ingest.PLATFORM_CONFIGS[platform]
    sql = ingest.BigQueryWriter._build_merge_sql("p.b.t", "p.b.t_tmp", cfg, _cols(platform), "2026-09-20", "2026-09-22")
    set_clause = sql.split("UPDATE SET", 1)[1].split("WHEN NOT MATCHED", 1)[0]
    updated = set(re.findall(r"target\.(\w+) = source\.", set_clause))
    assert updated == set(_cols(platform)) - {"date", "campaign_id", cfg.sub_group_col}
    assert "AND target.date BETWEEN '2026-09-20' AND '2026-09-22'" in sql


def test_restated_google_revenue_is_no_longer_dropped():
    sql = ingest.BigQueryWriter._build_merge_sql("t", "s", ingest.PLATFORM_CONFIGS["google"], _cols("google"), "2026-09-20", "2026-09-22")
    assert "target.conversion_value = source.conversion_value" in sql


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-09-22' OR TRUE --"])
def test_merge_rejects_non_iso_bounds(bad):
    with pytest.raises(ValueError):
        ingest.BigQueryWriter._build_merge_sql("t", "s", ingest.PLATFORM_CONFIGS["google"], _cols("google"), bad, "2026-09-22")


def test_merge_rejects_unsafe_column_names():
    with pytest.raises(ValueError):
        ingest.BigQueryWriter._build_merge_sql("t", "s", ingest.PLATFORM_CONFIGS["google"], _cols("google") + ["x = 1; DROP"], "2026-09-20", "2026-09-22")


class _Job:
    def __init__(self, rows=None):
        self._rows = rows or []

    def result(self, timeout=None):
        return self._rows


class _FakeClient:
    project = "p"

    def __init__(self):
        self.created, self.sql = [], []

    def load_table_from_dataframe(self, df, ref, job_config=None):
        return _Job()

    def get_table(self, ref):
        if ref.endswith("_staging_tmp"):
            return SimpleNamespace(schema=[bigquery.SchemaField("date", "STRING")])
        if not self.created:
            raise NotFound("absent")
        return self.created[-1]

    def create_table(self, table):
        self.created.append(table)

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        return _Job([SimpleNamespace(cnt=2)])


def test_new_bronze_table_is_clustered_and_merge_is_bounded():
    fake = _FakeClient()
    df = pd.DataFrame({"date": ["2026-09-21", "2026-09-22"], "campaign_id": ["fb_1", "fb_1"],
                       "ad_set_id": ["s", "s"], "spend": ["1.0", "2.0"]})
    ingest.BigQueryWriter(fake).merge_to_bronze(df, ingest.PLATFORM_CONFIGS["facebook"])
    assert fake.created[0].clustering_fields == ["date", "campaign_id"]
    assert any("BETWEEN '2026-09-21' AND '2026-09-22'" in s for s in fake.sql)
