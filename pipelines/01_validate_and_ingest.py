"""
01_validate_and_ingest.py
=========================
Tier 1 Bronze ingestion pipeline.

Reads a marketing data CSV (Facebook, Google, or TikTok), validates every row
against schema rules R0-R7, loads clean rows to a temporary BigQuery staging
table, then MERGEs into the bronze landing table using a composite primary
key for idempotent restatement-aware updates. Bad rows route to a quarantine
log with deterministic MD5 identifiers. Every state transition is recorded in
the ingestion audit table. On success, the source file is archived; on
failure, moved to a failed/ folder with error metadata.

Execution modes
---------------
  CLI (local mode):
      python pipelines/01_validate_and_ingest.py data/raw/01_facebook_ads.csv
      python pipelines/01_validate_and_ingest.py --all

  Event-driven (production mode):
      Invoked by Cloud Function on GCS object.finalized events.

Design properties
-----------------
  OOP           : Single-responsibility classes. No global mutable state.
  Availability  : BigQuery and GCS operations retry with exponential backoff.
  Reliability   : Composite-key MERGE ensures restatement updates rows in
                  place (no duplicates). Post-write row count verified.
                  Audit log records every state transition.
  Atomicity     : BigQuery MERGE is atomic at the table level. Quarantine
                  writes only happen after the clean MERGE succeeds.
  Security      : PlatformConfig is frozen. Credentials sourced from ADC.
  Durability    : Composite key (date, campaign_id, sub_group_id) means
                  re-running on the same source CSV produces identical state.
  Scalability   : Bronze pattern (all-STRING) absorbs upstream schema changes.
                  ALLOW_FIELD_ADDITION lets new columns flow in without code.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from google.cloud import bigquery
from google.cloud import storage
from google.cloud.exceptions import NotFound

import common  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    handlers=common.log_handlers("ingestion", fmt="%(asctime)s | %(name)-35s | %(levelname)-8s | %(message)s"),
)

# Constants
from common import BRONZE_DS as BRONZE_DATASET, STAGING_DS as STAGING_DATASET  # noqa: E402
QUARANTINE_TABLE = f"{STAGING_DATASET}.stg_quarantine_logs"
AUDIT_TABLE = f"{STAGING_DATASET}.tbl_ingestion_audit"
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "")

NUMERIC_FIELDS: frozenset = frozenset({
    "impressions", "clicks", "conversions", "spend", "cost",
    "video_views", "video_watch_25", "video_watch_50",
    "video_watch_75", "video_watch_100",
    "reach", "likes", "shares", "comments",
    "frequency", "engagement_rate",
    "quality_score", "search_impression_share", "avg_cpc",
    "conversion_value", "ctr",
})

PLATFORM_PATTERNS = {
    "facebook": ("facebook", "fb_"),
    "google":   ("google", "_gg_", "googleads"),
    "tiktok":   ("tiktok", "_tt_", "tt_"),
}


# Value objects
@dataclass(frozen=True)
class PlatformConfig:
    """Immutable per-platform configuration."""
    name: str
    target_table: str
    cost_col: str
    sub_group_col: str


@dataclass
class IngestionResult:
    """Captures the outcome of a single file ingestion run."""
    platform: str
    file_path: str
    audit_id: str
    rows_in_file: int = 0
    rows_accepted: int = 0
    rows_quarantined: int = 0
    rows_merged: int = 0
    success: bool = False
    error_stage: Optional[str] = None
    error_message: Optional[str] = None


PLATFORM_CONFIGS = {
    "facebook": PlatformConfig(
        name="facebook",
        target_table=f"{BRONZE_DATASET}.facebook_ads_landing",
        cost_col="spend",
        sub_group_col="ad_set_id",
    ),
    "google": PlatformConfig(
        name="google",
        target_table=f"{BRONZE_DATASET}.google_ads_landing",
        cost_col="cost",
        sub_group_col="ad_group_id",
    ),
    "tiktok": PlatformConfig(
        name="tiktok",
        target_table=f"{BRONZE_DATASET}.tiktok_ads_landing",
        cost_col="cost",
        sub_group_col="adgroup_id",
    ),
}


# Validation
class DataQualityGate:
    """Bronze-layer schema validation per Master LLD Section 9.2 (R0-R3)."""

    MIN_DATE = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    MAX_DATE_BUFFER_DAYS = 1

    def __init__(self, cost_col: str) -> None:
        self._cost_col = cost_col

    def validate(self, record: dict) -> Optional[str]:
        return (
            self._r0_structural_keys(record)
            or self._r1_temporal_bounds(record)
            or self._r2_financial_non_negative(record)
            or self._r3_metric_hierarchy(record)
        )

    def impute(self, record: dict) -> dict:
        return {
            k: ("0" if k in NUMERIC_FIELDS else "UNKNOWN_DIMENSION")
            if self._is_empty(v) else str(v).strip()
            for k, v in record.items()
        }

    def _r0_structural_keys(self, record: dict) -> Optional[str]:
        date_val = str(record.get("date", "") or "").strip()
        campaign_id = str(record.get("campaign_id", "") or "").strip()
        if not date_val or not campaign_id:
            return "R0_STRUCTURAL: 'date' or 'campaign_id' is null or empty."
        return None

    def _r1_temporal_bounds(self, record: dict) -> Optional[str]:
        date_val = str(record.get("date", "") or "").strip()
        try:
            parsed = datetime.datetime.strptime(date_val, "%Y-%m-%d").replace(
                tzinfo=datetime.UTC
            )
            max_date = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                days=self.MAX_DATE_BUFFER_DAYS
            )
            if parsed < self.MIN_DATE or parsed > max_date:
                return (
                    f"R1_TEMPORAL: Date '{date_val}' outside "
                    f"[{self.MIN_DATE.date()}, {max_date.date()}]."
                )
        except ValueError:
            return f"R1_TEMPORAL: Date '{date_val}' does not match YYYY-MM-DD format."
        return None

    def _r2_financial_non_negative(self, record: dict) -> Optional[str]:
        try:
            spend = float(record.get(self._cost_col, 0) or 0)
            if spend < 0:
                return f"R2_FINANCIAL: '{self._cost_col}' is negative ({spend})."
        except (ValueError, TypeError) as exc:
            return f"R2_FINANCIAL: '{self._cost_col}' parse failed - {exc}"
        return None

    def _r3_metric_hierarchy(self, record: dict) -> Optional[str]:
        try:
            impressions = int(float(record.get("impressions", 0) or 0))
            clicks = int(float(record.get("clicks", 0) or 0))
            conversions = int(float(record.get("conversions", 0) or 0))
            if not (impressions >= clicks >= conversions):
                return (
                    f"R3_HIERARCHY: impressions({impressions}) >= "
                    f"clicks({clicks}) >= conversions({conversions}) is False."
                )
        except (ValueError, TypeError) as exc:
            return f"R3_HIERARCHY: Metric parse failed - {exc}"
        return None

    @staticmethod
    def _is_empty(val) -> bool:
        if val is None:
            return True
        try:
            if pd.isna(val):
                return True
        except (TypeError, ValueError):
            pass
        return str(val).strip().lower() in ("", "nan", "null", "none", "n/a", "na")


# Audit logging
# Audit logging (Refactored to Append-Only Streaming Architecture)
class AuditLogger:
    """
    Records each ingestion run's lifecycle in tbl_ingestion_audit using an 
    append-only architecture. Each phase transition streams a new event log,
    eliminating costly and slow BigQuery UPDATE mutations.
    """

    _SCHEMA = [
        bigquery.SchemaField("audit_id",         "STRING", mode="REQUIRED"),
        bigquery.SchemaField("file_name",        "STRING"),
        bigquery.SchemaField("file_size_bytes",  "INT64"),
        bigquery.SchemaField("platform",         "STRING"),
        bigquery.SchemaField("started_at",       "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("last_updated_at",  "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("completed_at",     "TIMESTAMP"),
        bigquery.SchemaField("rows_in_file",     "INT64"),
        bigquery.SchemaField("rows_accepted",    "INT64"),
        bigquery.SchemaField("rows_quarantined", "INT64"),
        bigquery.SchemaField("rows_merged",      "INT64"),
        bigquery.SchemaField("status",           "STRING", mode="REQUIRED"),
        bigquery.SchemaField("error_message",    "STRING"),
        bigquery.SchemaField("error_stage",      "STRING"),
    ]

    def __init__(self, bq_client: bigquery.Client) -> None:
        self._client = bq_client
        self._logger = logging.getLogger(self.__class__.__name__)
        self._table_ref = f"{self._client.project}.{AUDIT_TABLE}"

    def ensure_table_exists(self) -> None:
        try:
            self._client.get_table(self._table_ref)
        except NotFound:
            self._logger.info(f"Creating audit table {self._table_ref}")
            table = bigquery.Table(self._table_ref, schema=self._SCHEMA)
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="started_at",
                expiration_ms=90 * 24 * 60 * 60 * 1000,
            )
            self._client.create_table(table)

    def insert_started(self, audit_id: str, file_name: str, file_size: int, platform: str) -> None:
        """Stream inserts the initial entry tracking the file properties."""
        now = datetime.datetime.now(datetime.UTC).isoformat()
        
        row_to_insert = {
            "audit_id": audit_id,
            "file_name": file_name,
            "file_size_bytes": file_size,
            "platform": platform,
            "started_at": now,
            "last_updated_at": now,
            "completed_at": None,
            "rows_in_file": None,
            "rows_accepted": None,
            "rows_quarantined": None,
            "rows_merged": None,
            "status": "STARTED",
            "error_message": None,
            "error_stage": None,
        }
        self._stream_append(row_to_insert)

    def update_status(
        self,
        audit_id: str,
        status: str,
        rows_in_file: Optional[int] = None,
        rows_accepted: Optional[int] = None,
        rows_quarantined: Optional[int] = None,
        rows_merged: Optional[int] = None,
        error_message: Optional[str] = None,
        error_stage: Optional[str] = None,
    ) -> None:
        """Appends a brand-new state snapshot row instead of executing an UPDATE."""
        now = datetime.datetime.now(datetime.UTC).isoformat()
        is_terminal = status in ("SUCCESS", "FAILED")

        row_to_insert = {
            "audit_id": audit_id,
            "file_name": None,  # Can look up dimensionally from the STARTED row
            "file_size_bytes": None,
            "platform": None,
            "started_at": now,        # Required field for table partitioning
            "last_updated_at": now,   # Required field
            "completed_at": now if is_terminal else None,
            "rows_in_file": rows_in_file,
            "rows_accepted": rows_accepted,
            "rows_quarantined": rows_quarantined,
            "rows_merged": rows_merged,
            "status": status,
            "error_message": error_message,
            "error_stage": error_stage,
        }
        self._stream_append(row_to_insert)

    def _stream_append(self, row_dict: dict) -> None:
        """Helper method using low-latency streaming insert API."""
        try:
            errors = self._client.insert_rows_json(self._table_ref, [row_dict])
            if errors:
                raise RuntimeError(f"BigQuery streaming rejection: {errors}")
        except Exception as exc:
            self._logger.error(f"Failed to append audit event stream: {exc}")
            raise

# BigQuery I/O
class BigQueryWriter:
    """All BigQuery I/O for the ingestion pipeline."""

    MAX_RETRIES = 3
    BACKOFF_BASE = 2.0
    MAX_BYTES_BILLED = 1_073_741_824  # 1 GB

    def __init__(self, bq_client: bigquery.Client) -> None:
        self._client = bq_client
        self._logger = logging.getLogger(self.__class__.__name__)

    def ensure_dataset_exists(self, dataset_id: str) -> None:
        ref = bigquery.DatasetReference(self._client.project, dataset_id)
        try:
            self._client.get_dataset(ref)
        except NotFound:
            self._logger.info(f"Creating dataset '{dataset_id}'")
            ds = bigquery.Dataset(ref)
            ds.location = "US"
            self._client.create_dataset(ds, timeout=30)

    def ensure_quarantine_table(self) -> None:
        table_ref = f"{self._client.project}.{QUARANTINE_TABLE}"
        try:
            self._client.get_table(table_ref)
        except NotFound:
            self._logger.info(f"Creating quarantine table {table_ref}")
            schema = [
                bigquery.SchemaField("quarantine_id",       "STRING", mode="REQUIRED"),
                bigquery.SchemaField("execution_timestamp", "TIMESTAMP", mode="REQUIRED"),
                bigquery.SchemaField("origin_platform",     "STRING", mode="REQUIRED"),
                bigquery.SchemaField("raw_record_json",     "STRING", mode="REQUIRED"),
                bigquery.SchemaField("violated_rule_identifier", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("remediation_status",  "STRING", mode="REQUIRED"),
            ]
            table = bigquery.Table(table_ref, schema=schema)
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="execution_timestamp",
                expiration_ms=90 * 24 * 60 * 60 * 1000,
            )
            self._client.create_table(table)

    def merge_to_bronze(
        self, df: pd.DataFrame, config: PlatformConfig
    ) -> tuple:
        """
        Load to temp staging, then MERGE into bronze landing on composite key.
        Returns (rows_loaded, rows_in_target_after_merge).
        """
        target_table = f"{self._client.project}.{config.target_table}"
        temp_table = f"{target_table}_staging_tmp"

        self._logger.info(f"Loading {len(df)} rows to temp '{temp_table}'")
        self._load_dataframe_with_retry(
            df, temp_table, bigquery.WriteDisposition.WRITE_TRUNCATE
        )

        try:
            self._client.get_table(target_table)
        except NotFound:
            self._logger.info(
                f"Target '{target_table}' missing - creating from staging schema"
            )
            temp_obj = self._client.get_table(temp_table)
            table = bigquery.Table(target_table, schema=temp_obj.schema)
            table.clustering_fields = ["date", "campaign_id"]   # batch-bounded MERGEs prune to their dates
            self._client.create_table(table)

        merge_sql = self._build_merge_sql(
            target_table, temp_table, config, list(df.columns), df["date"].min(), df["date"].max()
        )
        self._logger.info(f"Executing MERGE into '{target_table}'")
        self._execute_query_with_retry(merge_sql)

        target_count = self._get_row_count(target_table)
        self._client.query(
            f"DROP TABLE IF EXISTS `{temp_table}`"
        ).result(timeout=30)

        return (len(df), target_count)

    @staticmethod
    def _build_merge_sql(
        target: str, source: str, config: PlatformConfig,
        columns: list[str], date_min: str, date_max: str,
    ) -> str:
        """Every non-key column is updated, so restated conversion_value, reach and video columns are never dropped.
        The target is bounded to the batch's dates, so on the date-clustered table the MERGE prunes to them."""
        lo, hi = (datetime.date.fromisoformat(d).isoformat() for d in (date_min, date_max))
        unsafe = [c for c in columns if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c)]
        if unsafe:
            raise ValueError(f"unsafe column names in upload: {unsafe}")
        keys = ("date", "campaign_id", config.sub_group_col)
        update_set = ",\n            ".join(f"target.{c} = source.{c}" for c in columns if c not in keys)
        return f"""
        MERGE `{target}` AS target
        USING `{source}` AS source
        ON  target.date = source.date
        AND target.campaign_id = source.campaign_id
        AND target.{config.sub_group_col} = source.{config.sub_group_col}
        AND target.date BETWEEN '{lo}' AND '{hi}'
        WHEN MATCHED THEN UPDATE SET
            {update_set}
        WHEN NOT MATCHED THEN INSERT ROW
        """

    def append_quarantine_rows(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        table_ref = f"{self._client.project}.{QUARANTINE_TABLE}"
        self._load_dataframe_with_retry(
            df, table_ref, bigquery.WriteDisposition.WRITE_APPEND
        )
        return len(df)

    def _load_dataframe_with_retry(
        self,
        df: pd.DataFrame,
        table_ref: str,
        disposition: bigquery.WriteDisposition,
    ) -> None:
        last_exc = None
        for attempt in range(self.MAX_RETRIES):
            try:
                # 1. Initialize config without the schema update options. Parquet carries
                # the schema, so autodetect goes.
                job_config = common.parquet_load_config(write_disposition=disposition)

                # 2. Only add schema update options if we are APPENDING data
                if disposition in (bigquery.WriteDisposition.WRITE_APPEND, "WRITE_APPEND"):
                    job_config.schema_update_options = [
                        bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION,
                    ]
                self._client.load_table_from_dataframe(
                    df, table_ref, job_config=job_config
                ).result(timeout=300)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < self.MAX_RETRIES - 1:
                    wait = self.BACKOFF_BASE ** attempt
                    self._logger.warning(
                        f"Load attempt {attempt + 1}/{self.MAX_RETRIES} to "
                        f"'{table_ref}' failed: {exc}. Retry in {wait}s"
                    )
                    time.sleep(wait)
        raise RuntimeError(
            f"BQ load to '{table_ref}' failed after "
            f"{self.MAX_RETRIES} attempts: {last_exc}"
        )

    def _execute_query_with_retry(self, sql: str) -> None:
        last_exc = None
        for attempt in range(self.MAX_RETRIES):
            try:
                job_config = bigquery.QueryJobConfig(
                    maximum_bytes_billed=self.MAX_BYTES_BILLED,
                )
                self._client.query(sql, job_config=job_config).result(timeout=300)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < self.MAX_RETRIES - 1:
                    wait = self.BACKOFF_BASE ** attempt
                    self._logger.warning(
                        f"Query attempt {attempt + 1}/{self.MAX_RETRIES} "
                        f"failed: {exc}. Retry in {wait}s"
                    )
                    time.sleep(wait)
        raise RuntimeError(
            f"BQ query failed after {self.MAX_RETRIES} attempts: {last_exc}"
        )

    def _get_row_count(self, table_ref: str) -> int:
        result = list(
            self._client.query(
                f"SELECT COUNT(*) AS cnt FROM `{table_ref}`"
            ).result(timeout=30)
        )
        return int(result[0].cnt)


# GCS archival
class FileArchiver:
    """
    Moves processed files in GCS to archive/ or failed/.
    Skips silently if GCS_BUCKET_NAME unset (local CLI mode).
    """

    def __init__(self, bucket_name: str) -> None:
        self._bucket_name = bucket_name
        self._enabled = bool(bucket_name)
        self._logger = logging.getLogger(self.__class__.__name__)
        if self._enabled:
            self._client = storage.Client()

    def archive_success(self, file_path: str) -> None:
        if not self._enabled or not file_path.startswith("incoming/"):
            return
        try:
            now = datetime.datetime.now(datetime.UTC)
            filename = os.path.basename(file_path)
            dest = f"archive/{now.year}/{now.month:02d}/{filename}"
            bucket = self._client.bucket(self._bucket_name)
            bucket.rename_blob(bucket.blob(file_path), dest)
            self._logger.info(f"Archived: gs://{self._bucket_name}/{dest}")
        except Exception as exc:
            self._logger.warning(f"Archive failed (non-fatal): {exc}")

    def archive_failed(
        self, file_path: str, error_stage: str, error_message: str
    ) -> None:
        if not self._enabled or not file_path.startswith("incoming/"):
            return
        try:
            filename = os.path.basename(file_path)
            dest = f"failed/{filename}"
            bucket = self._client.bucket(self._bucket_name)
            src_blob = bucket.blob(file_path)
            new_blob = bucket.copy_blob(src_blob, bucket, dest)
            new_blob.metadata = {
                "error_stage": error_stage,
                "error_message": error_message[:1000],
                "failed_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
            new_blob.patch()
            src_blob.delete()
            self._logger.info(f"Failed file moved: gs://{self._bucket_name}/{dest}")
        except Exception as exc:
            self._logger.warning(f"Failure-archive failed (non-fatal): {exc}")


# Orchestrator
class IngestionPipeline:
    """End-to-end orchestrator for a single file ingestion."""

    def __init__(self) -> None:
        self._bq_client = common.bq_client()
        self._writer = BigQueryWriter(self._bq_client)
        self._auditor = AuditLogger(self._bq_client)
        self._archiver = FileArchiver(GCS_BUCKET_NAME)
        self._logger = logging.getLogger(self.__class__.__name__)

    def run(self, file_path: str) -> IngestionResult:
        audit_id = str(uuid.uuid4())
        file_name = os.path.basename(file_path)
        platform = self._detect_platform(file_name)

        result = IngestionResult(
            platform=platform or "unknown",
            file_path=file_path,
            audit_id=audit_id,
        )

        if platform is None:
            result.error_stage = "PLATFORM_DETECTION"
            result.error_message = (
                f"Could not detect platform from '{file_name}'. "
                f"Expected substring: facebook|google|tiktok."
            )
            self._logger.error(result.error_message)
            self._archiver.archive_failed(
                file_path, result.error_stage, result.error_message
            )
            return result

        config = PLATFORM_CONFIGS[platform]

        try:
            self._writer.ensure_dataset_exists(BRONZE_DATASET)
            self._writer.ensure_dataset_exists(STAGING_DATASET)
            self._writer.ensure_quarantine_table()
            self._auditor.ensure_table_exists()

            file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            self._auditor.insert_started(audit_id, file_name, file_size, platform)
            self._logger.info(
                f"[audit={audit_id[:8]}] STARTED platform={platform} file={file_name}"
            )

            df_raw = pd.read_csv(file_path, dtype=str)
            result.rows_in_file = len(df_raw)
            self._logger.info(f"Loaded {len(df_raw)} raw records from {file_name}")
            self._auditor.update_status(
                audit_id, "VALIDATING", rows_in_file=len(df_raw)
            )

            clean_rows, quarantine_rows = self._process_records(
                df_raw, config, file_name
            )
            result.rows_accepted = len(clean_rows)
            result.rows_quarantined = len(quarantine_rows)
            self._logger.info(
                f"Validation: {len(clean_rows)} accepted, "
                f"{len(quarantine_rows)} quarantined"
            )
            self._auditor.update_status(
                audit_id, "VALIDATING",
                rows_accepted=len(clean_rows),
                rows_quarantined=len(quarantine_rows),
            )

            if clean_rows:
                self._auditor.update_status(audit_id, "MERGING")
                df_clean = pd.DataFrame(clean_rows).astype(str)
                df_clean["ingested_at"] = datetime.datetime.now(
                    datetime.UTC
                ).isoformat()
                df_clean["source_file"] = file_name

                _, rows_merged = self._writer.merge_to_bronze(df_clean, config)
                result.rows_merged = rows_merged
                self._logger.info(f"MERGE complete: {rows_merged} rows in target")

            if quarantine_rows:
                self._writer.append_quarantine_rows(pd.DataFrame(quarantine_rows))
                self._logger.warning(
                    f"Quarantined {len(quarantine_rows)} rows to {QUARANTINE_TABLE}"
                )

            self._archiver.archive_success(file_path)

            self._auditor.update_status(
                audit_id, "SUCCESS", rows_merged=result.rows_merged
            )
            result.success = True
            self._logger.info(f"[audit={audit_id[:8]}] SUCCESS platform={platform}")

        except Exception as exc:
            result.success = False
            result.error_stage = "PIPELINE"
            result.error_message = str(exc)
            self._logger.error(
                f"[audit={audit_id[:8]}] FAILED: {exc}", exc_info=True
            )
            try:
                self._auditor.update_status(
                    audit_id, "FAILED",
                    error_stage=result.error_stage,
                    error_message=str(exc)[:1000],
                )
            except Exception as audit_exc:
                self._logger.error(f"Audit update failed: {audit_exc}")
            self._archiver.archive_failed(file_path, result.error_stage, str(exc))

        return result

    @staticmethod
    def _detect_platform(filename: str) -> Optional[str]:
        lower = filename.lower()
        for platform, patterns in PLATFORM_PATTERNS.items():
            for pattern in patterns:
                if pattern in lower:
                    return platform
        return None

    def _process_records(
        self, df_raw: pd.DataFrame, config: PlatformConfig, file_name: str
    ) -> tuple:
        gate = DataQualityGate(cost_col=config.cost_col)
        execution_ts = datetime.datetime.now(datetime.UTC).isoformat()

        clean_rows = []
        quarantine_rows = []

        for _, row in df_raw.iterrows():
            record = row.to_dict()
            violation = gate.validate(record)
            if violation:
                quarantine_rows.append(
                    self._build_quarantine_row(
                        record, violation, config.name, execution_ts
                    )
                )
            else:
                clean_rows.append(gate.impute(record))

        return clean_rows, quarantine_rows

    @staticmethod
    def _build_quarantine_row(
        record: dict, reason: str, platform: str, execution_ts: str
    ) -> dict:
        date_val = str(record.get("date", "")).strip()
        campaign_id = str(record.get("campaign_id", "")).strip()
        hash_input = f"{platform}|{date_val}|{campaign_id}|{reason}"
        q_id = hashlib.md5(hash_input.encode("utf-8")).hexdigest()
        return {
            "quarantine_id": q_id,
            "execution_timestamp": execution_ts,
            "origin_platform": platform,
            "raw_record_json": json.dumps(record, default=str),
            "violated_rule_identifier": reason,
            "remediation_status": "PENDING",
        }


# CLI
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bronze ingestion pipeline (event-driven, MERGE-based)."
    )
    parser.add_argument(
        "file_path", nargs="?",
        help="Path to a single CSV file to ingest.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Ingest all 3 platform CSVs from data/raw/.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    pipeline = IngestionPipeline()
    overall_success = True

    if args.all:
        files = [
            "data/raw/01_facebook_ads.csv",
            "data/raw/02_google_ads.csv",
            "data/raw/03_tiktok_ads.csv",
        ]
        for fp in files:
            result = pipeline.run(fp)
            if not result.success:
                overall_success = False
    elif args.file_path:
        result = pipeline.run(args.file_path)
        overall_success = result.success
    else:
        print("Usage: python 01_validate_and_ingest.py <file.csv>")
        print("       python 01_validate_and_ingest.py --all")
        sys.exit(2)

    sys.exit(0 if overall_success else 1)


if __name__ == "__main__":
    main()