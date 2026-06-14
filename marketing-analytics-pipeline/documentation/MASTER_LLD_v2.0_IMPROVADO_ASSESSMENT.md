# Master Low-Level Design Document (v2.0)
## Improvado Senior Marketing Analyst — Technical Assessment
### Event-Driven Cross-Channel Marketing Analytics Lakehouse

---

> **Document purpose:** This is the single source of truth for executing the
> complete pipeline end-to-end. Every file path, every SQL statement, every
> validation rule, every dashboard widget, every Cloud Function configuration
> is defined here. Execute sections in order. Do not skip verification steps.
>
> **Version 2.0 changes from v1.0:**
> - Two-tier architecture: Tier 1 (event-driven ingestion + transformation),
>   Tier 2 (scheduled / on-demand derived analytics)
> - MERGE-based incremental loads replace WRITE_TRUNCATE everywhere it would
>   destroy historical data
> - Composite key idempotency: `(date, campaign_id, sub_group_id)`
> - Eventarc + Cloud Function 2nd gen trigger pattern for GCS object events
> - Complete Python + SQL validation matrix per column
> - Operational layer: file archival, audit logging, cost guardrails,
>   retention policies, schema evolution policy
> - Production constraints documented for Phase 2 evolution

---

## Table of Contents

1.  [Project Context & Business Problem](#1-project-context--business-problem)
2.  [Ground-Truth Data Findings](#2-ground-truth-data-findings)
3.  [Two-Tier System Architecture](#3-two-tier-system-architecture)
4.  [Naming Conventions](#4-naming-conventions)
5.  [Repository Structure](#5-repository-structure)
6.  [Environment Setup](#6-environment-setup)
7.  [Tier 1 — Step 1: Bronze Ingestion (Event-Driven, MERGE)](#7-tier-1--step-1-bronze-ingestion-event-driven-merge)
8.  [Tier 1 — Step 2: Gold Transformation (MERGE)](#8-tier-1--step-2-gold-transformation-merge)
9.  [Data Validation Matrix (Bronze + Gold)](#9-data-validation-matrix-bronze--gold)
10. [Operational Layer](#10-operational-layer)
11. [Tier 2 — Step 3: Anomaly Detection (90-day Window)](#11-tier-2--step-3-anomaly-detection-90-day-window)
12. [Tier 2 — Step 4: Budget Optimizer (30-day Window)](#12-tier-2--step-4-budget-optimizer-30-day-window)
13. [Tier 2 — Step 5: Forecast (60-day Window)](#13-tier-2--step-5-forecast-60-day-window)
14. [Tier 2 — Step 6: GenAI Executive Summary (Model-Agnostic)](#14-tier-2--step-6-genai-executive-summary-model-agnostic)
15. [Step 7: QA Validation Runner](#15-step-7-qa-validation-runner)
16. [Step 8: Cloud Function Deployment (Eventarc Trigger)](#16-step-8-cloud-function-deployment-eventarc-trigger)
17. [Step 9: Dashboard Specification (Streamlit — As Built)](#17-step-9-dashboard-specification-streamlit--as-built)
18. [Step 10: README + Step 11: Video Script](#18-step-10-readme--step-11-video-script)
19. [Submission Checklist](#19-submission-checklist)
20. [Production Constraints (Phase 2 Evolution)](#20-production-constraints-phase-2-evolution)
21. [Appendix A — Campaign-Level Performance Reference](#appendix-a--campaign-level-performance-reference)
22. [Appendix B — Complete Column Mapping Matrix](#appendix-b--complete-column-mapping-matrix)

---

## 1. Project Context & Business Problem

### 1.1 The Real Business Problem

A marketing team spending **$130,244.90/month** across Facebook, Google, and TikTok
has no unified performance view. Each platform uses different metric names, different
conversion definitions, and different reporting structures. The CMO cannot answer
four fundamental questions without manual spreadsheet reconciliation:

1.  Which channel has the lowest cost per acquisition?
2.  Is performance improving or degrading week over week?
3.  Where should the next incremental dollar go?
4.  What is our blended ROAS across all channels?

This pipeline solves the problem end-to-end as an **event-driven lakehouse** —
ingestion, unification, ML enrichment, and executive-ready visualization — exactly
what Improvado sells at enterprise scale.

### 1.2 Assignment Deliverables

| Deliverable | Required | Differentiator |
|---|---|---|
| Cloud database: 3 raw + 1 unified table | Yes | BigQuery, MERGE-based incremental |
| Live dashboard link | Yes | Streamlit (multi-page app, 5 pages — see Section 17) |
| Video walkthrough link | Yes | Loom, 5–8 min |
| GitHub repository | No | Critical — shows methodology |
| ML enrichment layer | No | Anomaly + optimizer + forecast |
| GenAI executive summary | No | Model-agnostic via REST |
| Cloud Function deployment code | No | Production-grade event trigger pattern |

---

## 2. Ground-Truth Data Findings

> Verified facts from profiling the actual CSV files. All code and dashboard
> specifications reference these exact numbers. Do not modify.

### 2.1 Source File Specifications

| Property | Facebook | Google | TikTok |
|---|---|---|---|
| File | `01_facebook_ads.csv` | `02_google_ads.csv` | `03_tiktok_ads.csv` |
| Rows | 110 | 110 | 110 |
| Date range | 2024-01-01 to 2024-01-30 | 2024-01-01 to 2024-01-30 | 2024-01-01 to 2024-01-30 |
| Unique campaigns | 4 | 4 | 4 |
| Nulls in any column | 0 | 0 | 0 |
| Duplicate rows | 0 | 0 | 0 |
| Grain | `(date, campaign_id, ad_set_id)` | `(date, campaign_id, ad_group_id)` | `(date, campaign_id, adgroup_id)` |

### 2.2 Financial Reconciliation Checkpoints (hardcoded for QA)

| Platform | Total Spend (Source) | Total Conversions | CPA |
|---|---|---|---|
| Facebook | **$18,292.00** | 2,395 | **$7.64** |
| Google | **$37,686.20** | 4,218 | **$8.93** |
| TikTok | **$74,266.70** | 6,750 | **$11.00** |
| **Grand Total** | **$130,244.90** | **13,363** | **$9.75 blended** |

### 2.3 Platform Efficiency Profile

| Metric | Facebook | Google | TikTok |
|---|---|---|---|
| Budget share | 14% | 29% | **57%** |
| CPA | **$7.64 (best)** | $8.93 | $11.00 (worst) |
| CTR | 1.96% | 1.90% | 1.61% |
| CPC | **$0.21** | $0.27 | $0.16 |
| CPM | $4.03 | $5.22 | **$2.59 (cheapest reach)** |
| Conversion rate | 2.69% | 3.07% | 1.46% |

**Primary insight:** TikTok receives 57% of total budget but delivers the worst CPA.
Facebook receives 14% but delivers the best CPA.

### 2.4 Campaign-Level CPA — Full Reference (Appendix A has full table)

| Best | Worst |
|---|---|
| Google Search_Brand_Terms: **$5.10** | Google Search_Generic_Terms: **$24.80** |

4.9x CPA gap within the same platform — and Generic gets 2x the budget of Brand.

### 2.5 Budget Optimizer Expected Output

With 2x-current-spend cap and 10% minimum floor (verified via scipy linprog):

| Platform | Current | Recommended | Conversion Delta |
|---|---|---|---|
| Facebook | $18,292 (14%) | $36,584 (28%) | +2,395 |
| Google | $37,686 (29%) | $75,372 (58%) | +4,220 |
| TikTok | $74,267 (57%) | $18,289 (14%) | -5,088 |
| **Total** | $130,245 | $130,245 | **+1,526 (+11.4%)** |

---

## 3. Two-Tier System Architecture

### 3.1 Tier Definitions

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                                                              │
│  GCS Bucket: improvado-analytics-lakehouse-raw                               │
│  ├─ incoming/         ← Upload CSVs here. Eventarc watches this.             │
│  ├─ archive/YYYY/MM/  ← Successfully processed files move here               │
│  └─ failed/           ← Files that failed ingestion (with error metadata)    │
│                                                                              │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   │  google.cloud.storage.object.v1.finalized
                                   │  (Eventarc, ~2-10s latency)
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  TIER 1 — EVENT-DRIVEN INGESTION                                             │
│  (Cloud Function 2nd gen via Eventarc, OR manual CLI for assignment)         │
│                                                                              │
│  Step 01: validate_and_ingest.py                                             │
│   ├─ Platform detection from filename (substring match)                      │
│   ├─ Audit log: STARTED                                                      │
│   ├─ Schema validation (Python: types, structural, ranges)                   │
│   ├─ Load to BQ staging table (_staging_tmp)                                 │
│   ├─ Audit log: VALIDATING                                                   │
│   ├─ MERGE → bronze landing on (date, campaign_id, sub_group_id)             │
│   ├─ Audit log: MERGING                                                      │
│   ├─ Drop staging table                                                      │
│   ├─ Move file: incoming/ → archive/YYYY/MM/  (on success)                   │
│   ├─                       OR → failed/ (on error, with metadata)            │
│   └─ Audit log: SUCCESS or FAILED                                            │
│                                                                              │
│  Step 02: run_transformations.py (auto-chained or manual)                    │
│   ├─ Audit log: STARTED                                                      │
│   ├─ Detect first-run vs incremental                                         │
│   ├─ First run: CREATE TABLE w/ partition+cluster                            │
│   ├─ Incremental: MERGE bronze → gold (per platform)                         │
│   ├─ Gold-layer business validation (SQL CTE filters/flags bad rows)         │
│   └─ Audit log: SUCCESS / FAILED                                             │
│                                                                              │
│  Result: Bronze + Gold always current, full history preserved                │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   │  Scheduled (Cloud Scheduler) or on-demand
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  TIER 2 — DERIVED ANALYTICS (WRITE_TRUNCATE on lookback windows)             │
│                                                                              │
│  Step 03: anomaly_detection.py    — last 90 days                             │
│  Step 04: budget_optimizer.py     — last 30 days                             │
│  Step 05: forecast.py             — last 60 days                             │
│  Step 06: llm_executive_summary.py — most recent state                       │
│                                                                              │
│  All Tier 2 scripts:                                                         │
│   - Read from gold mart with date-range WHERE clause (partition pruning)     │
│   - Use maximum_bytes_billed guardrail                                       │
│   - Write current-state outputs (WRITE_TRUNCATE — derived layer)             │
│   - Configurable LOOKBACK_DAYS at class level                                │
│                                                                              │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
                          Streamlit Dashboard
                  (reads current Tier 2 + Gold state directly via
                   google-cloud-bigquery; see Section 17)
```

### 3.2 Why Two Tiers

| Layer | Pattern | Why |
|---|---|---|
| Bronze + Gold | **MERGE incremental** | Historical fact data. Must preserve all months. Restatement-aware. |
| Anomaly + Optimizer + Forecast + LLM | **WRITE_TRUNCATE** | Derived outputs. Always recomputed from gold. Re-runs are cheap and deterministic. |
| Quarantine | **WRITE_APPEND with deterministic MD5** | Audit log of bad rows. Idempotent via MD5 keying. |
| Audit log | **WRITE_APPEND with UUID + status transitions** | Operational observability. One row per ingestion run. |

### 3.3 Architecture Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Cloud database | BigQuery | Free tier, native `google-cloud-bigquery` Python client (used directly by Streamlit), partition+cluster optimization, ADC auth, per-query `maximum_bytes_billed` cost guardrails |
| Trigger | Eventarc + Cloud Function 2nd gen | True event-driven (vs poll-based). GCS-native `object.finalized` event. |
| Bronze pattern | All-STRING landing | Insulates pipeline from upstream type changes (Improvado real ETL pattern) |
| Schema evolution | Permissive Bronze, Strict Gold | Bronze absorbs new upstream columns; Gold contract stays at 30 columns |
| Anomaly method | Z-score (rolling 7-day, per group) | Defensible at 30 days; interpretable; bidirectional |
| Forecast | Holt-Winters with SES fallback | Captures weekly seasonality; SES fallback for sparse campaigns |
| Optimizer | scipy linprog (HiGHS) | Lightweight LP; produces actionable CMO output |
| GenAI | OpenAI-compatible REST (model-agnostic) | Works with Groq / OpenRouter / Gemini / OpenAI / local Ollama |
| Secret storage | Env var for assignment; Secret Manager for production | Documented in deployment appendix |

### 3.4 What Is NOT in This Architecture (intentionally)

| Removed | Why |
|---|---|
| GCS data lake "Silver" layer | We have Bronze → Gold. Adding Silver adds complexity with no value for this scope. |
| Real-time streaming | Marketing data is batch (daily/monthly). |
| CI/CD pipeline | Out of scope for 3-day assessment. Documented in Section 20 as Phase 2. |
| Unit / integration tests | Out of scope. Phase 2. |
| Data catalog (Atlan, DataHub) | Useful at 100+ tables. We have 8. |

---

## 4. Naming Conventions

**Every file, every script, every SQL query uses exactly these names. Deviation
causes TableNotFound errors.**

| Layer | BigQuery Dataset |
|---|---|
| Bronze | `improvado_analytics_bronze` |
| Staging | `improvado_analytics_staging` |
| Production | `improvado_analytics_production` |

| Table | Full Reference | Pattern |
|---|---|---|
| Facebook landing | `improvado_analytics_bronze.facebook_ads_landing` | MERGE incremental |
| Google landing | `improvado_analytics_bronze.google_ads_landing` | MERGE incremental |
| TikTok landing | `improvado_analytics_bronze.tiktok_ads_landing` | MERGE incremental |
| Quarantine log | `improvado_analytics_staging.stg_quarantine_logs` | APPEND, MD5 dedupe |
| Ingestion audit | `improvado_analytics_staging.tbl_ingestion_audit` | APPEND, status transitions |
| Anomaly flags | `improvado_analytics_staging.fct_anomaly_flags` | TRUNCATE on 90d window |
| Budget recs | `improvado_analytics_staging.tbl_budget_recommendations` | TRUNCATE on 30d window |
| Forecast | `improvado_analytics_staging.tbl_forecast` | TRUNCATE on 60d window |
| AI summary | `improvado_analytics_staging.tbl_ai_executive_summary` | TRUNCATE, single row |
| Gold mart | `improvado_analytics_production.fct_unified_marketing_performance` | MERGE incremental |

### 4.1 GCS Bucket Layout

```
gs://improvado-analytics-lakehouse-raw/
  ├── incoming/                  ← Upload CSVs here (triggers pipeline)
  ├── archive/
  │   ├── 2024/01/               ← Files successfully processed in Jan 2024
  │   ├── 2024/02/
  │   └── ...
  └── failed/                    ← Files that failed validation/load
```

---

## 5. Repository Structure

```
marketing-analytics-pipeline/
├── .github/
│   └── workflows/
│       └── .gitkeep                      ← CI/CD documented but not deployed
├── cloud_function/                       ← NEW in v2.0
│   ├── main.py                           ← Cloud Function 2nd gen entry point
│   ├── requirements.txt                  ← Function-specific deps
│   └── cloudbuild.yaml                   ← Deployment config
├── config/
│   ├── platform_schema_registry.json     ← Per-platform column metadata + rules
│   └── validation_bounds_criteria.json   ← Range thresholds per column
├── data/
│   └── raw/
│       ├── 01_facebook_ads.csv
│       ├── 02_google_ads.csv
│       └── 03_tiktok_ads.csv
├── documentation/
│   ├── architecture_runbook_guide.md     ← Operational handbook
│   └── eventarc_deployment_guide.md      ← How to deploy the trigger
├── logs/                                 ← Generated at runtime
├── pipelines/
│   ├── 01_validate_and_ingest.py         ← Tier 1: Bronze MERGE
│   ├── 02_run_transformations.py         ← Tier 1: Gold MERGE
│   ├── 03_anomaly_detection.py           ← Tier 2: 90-day Z-score
│   ├── 04_budget_optimizer.py            ← Tier 2: 30-day linprog
│   ├── 05_forecast.py                    ← Tier 2: 60-day Holt-Winters
│   ├── 06_llm_executive_summary.py       ← Tier 2: Model-agnostic LLM
│   └── 07_qa_validation.py               ← QA reconciliation
├── queries/
│   ├── bronze_raw_schemas.sql            ← Bronze DDL (documentation)
│   ├── gold_unified_performance_mart.sql ← Gold transformation
│   └── qa_reconciliation_checks.sql      ← QA queries
├── requirements.txt
└── README.md
```

---

## 6. Environment Setup

### 6.1 requirements.txt

```text
pandas>=2.0.0
pandas-gbq>=0.19.0
pyarrow>=12.0.0
db-dtypes>=1.1.0
google-cloud-bigquery>=3.11.0
google-cloud-storage>=2.10.0
google-cloud-bigquery-storage>=2.22.0
scikit-learn>=1.3.0
scipy>=1.11.0
statsmodels>=0.14.0
requests>=2.31.0
numpy>=1.24.0
```

### 6.2 Installation + GCP Auth

```bash
cd ~/projects/improvado-sr-mrkt-assmt/marketing-analytics-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# GCP authentication via ADC
gcloud auth application-default login
gcloud config set project improvado-analytics-lakehouse

# Verify
python3 -c "from google.cloud import bigquery; print('Project:', bigquery.Client().project)"
```

### 6.3 Environment Variables

```bash
export GOOGLE_CLOUD_PROJECT="improvado-analytics-lakehouse"

# LLM (model-agnostic — use any OpenAI-compatible endpoint)
export LLM_ENDPOINT_URL="https://api.groq.com/openai/v1/chat/completions"
export LLM_BEARER_TOKEN="your_api_key_here"
export LLM_TARGET_MODEL="llama-3.1-70b-versatile"

# GCS bucket (for Tier 1 archival pattern)
export GCS_BUCKET_NAME="improvado-analytics-lakehouse-raw"
```

### 6.4 Pre-Refactor Cleanup (from v1.0 → v2.0)

If running v2.0 against a project that previously ran v1.0 scripts, drop the
old tables to start clean:

```bash
bq rm -r -f improvado-analytics-lakehouse:improvado_analytics_bronze
bq rm -r -f improvado-analytics-lakehouse:improvado_analytics_staging
bq rm -r -f improvado-analytics-lakehouse:improvado_analytics_production
bq rm -r -f improvado-analytics-lakehouse:mktg_analytics_staging  # legacy v0 name
bq ls --project_id=improvado-analytics-lakehouse  # confirm empty
```

The new pipeline auto-creates all required datasets on first run.

---

## 7. Tier 1 — Step 1: Bronze Ingestion (Event-Driven, MERGE)

### 7.1 Behavior Specification

The script must handle **all eight scenarios** from the design walkthrough:

| Scenario | Expected Behavior |
|---|---|
| First-ever load | Create bronze table, insert all rows |
| Same file re-uploaded | MERGE detects existing keys → updates with same values → no duplicates |
| Next-month data arrives | MERGE detects new keys → inserts → preserves prior months |
| Late-arriving restatement | MERGE detects existing keys with updated values → updates in place |
| Partial file failure | Good rows MERGE; bad rows quarantine with reason; pipeline succeeds |
| Concurrent uploads | Independent function instances per file; gold MERGE serializes via BigQuery |
| Trigger retry | Idempotent — re-running produces identical state |
| Schema change in source | Permissive Bronze: new columns accepted via `ALLOW_FIELD_ADDITION` |

### 7.2 Composite Primary Key

Every bronze landing table uses the same composite key for MERGE:

| Platform | Composite Key |
|---|---|
| Facebook | `(date, campaign_id, ad_set_id)` |
| Google | `(date, campaign_id, ad_group_id)` |
| TikTok | `(date, campaign_id, adgroup_id)` |

> **Note:** In the actual sample dataset, `(date, campaign_id)` alone is unique
> (only one sub_group per campaign per day). The third key element is **defensive
> over-specification** — supports future ad-level grain without schema change.

### 7.3 Audit Logging Status Transitions

Every script run logs to `improvado_analytics_staging.tbl_ingestion_audit` at each
phase. Schema:

```sql
CREATE TABLE improvado_analytics_staging.tbl_ingestion_audit (
    audit_id            STRING NOT NULL,            -- UUID per ingestion run
    file_name           STRING,
    file_size_bytes     INT64,
    platform            STRING,
    started_at          TIMESTAMP NOT NULL,
    last_updated_at     TIMESTAMP NOT NULL,
    completed_at        TIMESTAMP,
    rows_in_file        INT64,
    rows_accepted       INT64,
    rows_quarantined    INT64,
    rows_merged         INT64,
    status              STRING NOT NULL,            -- STARTED, VALIDATING, MERGING, SUCCESS, FAILED
    error_message       STRING,
    error_stage         STRING                      -- which phase failed
)
PARTITION BY DATE(started_at)
OPTIONS(partition_expiration_days = 90);
```

Status flow:
```
STARTED → VALIDATING → MERGING → SUCCESS
                                ↘ FAILED (at any stage)
```

Each transition is a separate `UPDATE` on the audit row (matched by `audit_id`).

### 7.4 File Archival Behavior

After ingestion completes:

- **SUCCESS:** Move file `gs://bucket/incoming/<file>` → `gs://bucket/archive/YYYY/MM/<file>` where YYYY/MM = current UTC year/month
- **FAILED:** Move file to `gs://bucket/failed/<file>` with object metadata containing `error_stage` and `error_message`

This prevents accidental re-processing of already-handled files and creates an auditable trail.

### 7.5 Validation Rules (Python, Bronze Layer)

Detailed in Section 9. Bronze validation is **schema-level**: types parse correctly, structural keys present, basic non-negativity. **Range validation** (e.g., `quality_score ∈ [1,10]`) happens in the Gold SQL layer (Section 8).

### 7.6 MERGE Pattern (SQL Template)

The script writes incoming rows to a temporary staging table, then issues this MERGE:

```sql
MERGE `improvado_analytics_bronze.facebook_ads_landing` AS target
USING `improvado_analytics_bronze.facebook_ads_landing_staging_tmp` AS source
ON  target.date = source.date
AND target.campaign_id = source.campaign_id
AND target.ad_set_id = source.ad_set_id
WHEN MATCHED THEN UPDATE SET
    target.impressions = source.impressions,
    target.clicks = source.clicks,
    target.spend = source.spend,
    target.conversions = source.conversions,
    target.video_views = source.video_views,
    target.engagement_rate = source.engagement_rate,
    target.reach = source.reach,
    target.frequency = source.frequency,
    target.campaign_name = source.campaign_name,
    target.ad_set_name = source.ad_set_name,
    target.ingested_at = source.ingested_at,
    target.source_file = source.source_file
WHEN NOT MATCHED THEN INSERT ROW;
```

After MERGE: `DROP TABLE` on the staging temp table.

### 7.7 New Bronze Schema Columns (added in v2.0)

Every bronze table now has two diagnostic columns:

- `ingested_at TIMESTAMP` — when this specific row was loaded (UTC)
- `source_file STRING` — which CSV the row came from

These are audit-only — never referenced by Gold transformations or analytics.

### 7.8 Cost Guardrails

Every BigQuery operation in the script sets:

```python
job_config = bigquery.LoadJobConfig(
    ...,
    maximum_bytes_billed=1_073_741_824,  # 1 GB cap per job
)
```

Prevents runaway costs from misconfigured queries.

### 7.9 Platform Detection by Filename

The script accepts a file path as argument (or `latest in incoming/` if none provided). Platform is detected by substring match (case-insensitive):

```python
if "facebook" in filename.lower() or "fb_" in filename.lower():
    platform = "facebook"
elif "google" in filename.lower() or "_gg_" in filename.lower():
    platform = "google"
elif "tiktok" in filename.lower() or "_tt_" in filename.lower():
    platform = "tiktok"
else:
    # File moved to failed/ with error_stage = "PLATFORM_DETECTION"
    raise UnknownPlatformError(filename)
```

### 7.10 Execution

```bash
# CLI invocation (assignment mode)
python pipelines/01_validate_and_ingest.py data/raw/01_facebook_ads.csv

# Or batch all files
python pipelines/01_validate_and_ingest.py --all

# In production: invoked by Cloud Function with the GCS event payload
```

### 7.11 Expected Output (first run, January data)

```
Audit run: 7f3a... | STARTED  | facebook
Audit run: 7f3a... | VALIDATING | 110 rows in file
Audit run: 7f3a... | VALIDATING | 110 accepted, 0 quarantined
Audit run: 7f3a... | MERGING  | staging → bronze
Audit run: 7f3a... | MERGING  | merged 110 rows
Audit run: 7f3a... | SUCCESS  | file archived to gs://...archive/2024/01/
```

---

## 8. Tier 1 — Step 2: Gold Transformation (MERGE)

### 8.1 Behavior Specification

| Scenario | Behavior |
|---|---|
| Gold table does not exist | `CREATE TABLE IF NOT EXISTS` with 24 NOT NULL base columns + 6 nullable SAFE\_DIVIDE derived columns. `CREATE OR REPLACE` was explicitly rejected — it drops NOT NULL constraints on re-run. |
| Gold table exists, incremental run | `MERGE` source bronze data into gold on composite key |
| Bronze data updated via restatement | Existing gold row UPDATE with corrected values |
| New platform added | Inserts new platform rows; existing platforms unchanged |

### 8.2 Composite Key for Gold MERGE

`(date, platform, campaign_id, sub_group_id)` — note `platform` is needed here
because the gold table unifies all three platforms into one table.

### 8.3 Two Execution Paths

The script auto-detects which path to take:

```python
def run(self) -> None:
    if not self._gold_table_exists():
        # First-run path: DDL only, then MERGE against empty table
        self.create_gold_schema()       # CREATE TABLE IF NOT EXISTS (DDL)
    # Both paths then run incremental MERGE
    self.incremental_transaction()      # quarantine MERGEs × 3 + gold MERGE × 3
```

### 8.4 First-Run Path (CREATE TABLE IF NOT EXISTS)

On first run (gold table absent), `create_gold_schema()` issues DDL only:

```sql
CREATE TABLE IF NOT EXISTS
  `improvado_analytics_production.fct_unified_marketing_performance`
(
  -- 24 base columns, all NOT NULL
  date                     DATE        NOT NULL,
  platform                 STRING      NOT NULL,
  campaign_id              STRING      NOT NULL,
  campaign_name            STRING      NOT NULL,
  sub_group_id             STRING      NOT NULL,
  sub_group_name           STRING      NOT NULL,
  impressions              INT64       NOT NULL,
  clicks                   INT64       NOT NULL,
  spend                    FLOAT64     NOT NULL,
  conversions              INT64       NOT NULL,
  conversion_value         FLOAT64     NOT NULL,
  video_views              INT64       NOT NULL,
  video_watch_25           INT64       NOT NULL,
  video_watch_50           INT64       NOT NULL,
  video_watch_75           INT64       NOT NULL,
  video_watch_100          INT64       NOT NULL,
  likes                    INT64       NOT NULL,
  shares                   INT64       NOT NULL,
  comments                 INT64       NOT NULL,
  reach                    INT64       NOT NULL,
  frequency                FLOAT64     NOT NULL,
  quality_score            INT64       NOT NULL,
  search_impression_share  FLOAT64     NOT NULL,
  ingested_at              TIMESTAMP   NOT NULL,
  source_file              STRING      NOT NULL,

  -- 6 nullable derived columns (SAFE_DIVIDE can produce NULL)
  cpa                      FLOAT64,
  ctr                      FLOAT64,
  cpc                      FLOAT64,
  cpm                      FLOAT64,
  engagement_rate          FLOAT64,
  roas                     FLOAT64
)
PARTITION BY date
CLUSTER BY platform, campaign_id
OPTIONS(description='Gold unified marketing performance mart — v2.0');
```

After the DDL, `incremental_transaction()` runs immediately against the now-empty
table — inserting all rows via MERGE. There is no separate "bulk load" path.

**Why NOT `CREATE OR REPLACE TABLE`:** It silently drops all NOT NULL constraints
on every re-run, making the schema contract unenforced after the first execution.
`CREATE TABLE IF NOT EXISTS` preserves the constraints and is a no-op on
subsequent runs.

### 8.5 Incremental MERGE Path (NEW in v2.0)

Per-platform MERGE statements wrapped in a transaction:

```sql
BEGIN TRANSACTION;

-- ── Facebook MERGE ─────────────────────────────────────────────────────────
MERGE `improvado_analytics_production.fct_unified_marketing_performance` AS target
USING (
    -- Same transformation logic as v1.0 fb_processed CTE, plus business
    -- validation CTE (see Section 9.4)
    SELECT ... FROM `improvado_analytics_bronze.facebook_ads_landing`
    WHERE date IS NOT NULL AND campaign_id IS NOT NULL
) AS source
ON  target.date = source.date
AND target.platform = source.platform
AND target.campaign_id = source.campaign_id
AND target.sub_group_id = source.sub_group_id
WHEN MATCHED THEN UPDATE SET
    target.impressions = source.impressions,
    target.clicks = source.clicks,
    target.spend = source.spend,
    ... (all 30 columns)
WHEN NOT MATCHED THEN INSERT ROW;

-- ── Google MERGE ───────────────────────────────────────────────────────────
MERGE ... AS target
USING ( SELECT ... FROM `improvado_analytics_bronze.google_ads_landing` ... )
... ;

-- ── TikTok MERGE ───────────────────────────────────────────────────────────
MERGE ... AS target
USING ( SELECT ... FROM `improvado_analytics_bronze.tiktok_ads_landing` ... )
... ;

COMMIT TRANSACTION;
```

If any platform MERGE fails, the transaction rolls back. All-or-nothing semantics.

### 8.6 Gold Layer Business Validation (NEW in v2.0)

Before the MERGE writes a row to gold, a validation CTE filters out business-invalid
rows (Section 9.4 has the complete validation matrix). Rejected rows are inserted
into `improvado_analytics_staging.stg_quarantine_logs` with `origin_platform =
'<platform>_gold_validation'` and a specific `violated_rule_identifier`.

### 8.7 Post-Execution Verification

After MERGE completes:

- **Row count check:** Sum of bronze rows per platform == platform rows in gold
- **Column count check:** Exactly 30 columns (Data Contract — hard fail)
- **Financial reconciliation:** Total bronze spend per platform == total gold spend per platform (to within $0.01)

### 8.8 Execution

```bash
python pipelines/02_run_transformations.py
# Auto-detects first-run vs incremental
```

### 8.9 SqlBuilder and _PLATFORM_CONFIG (DRY Architecture)

All SQL for the gold transformation is generated by a `SqlBuilder` class
driven by a single `_PLATFORM_CONFIG` dictionary. No SQL is hardcoded in
multiple places.

```python
_PLATFORM_CONFIG = {
    "Facebook": {
        "bronze_table": "improvado_analytics_bronze.facebook_ads_landing",
        "platform_name": "Facebook",
        "has_video":      True,
        "has_quality":    False,
        "has_reach":      True,
    },
    "Google": {
        "bronze_table": "improvado_analytics_bronze.google_ads_landing",
        "platform_name": "Google",
        "has_video":      False,
        "has_quality":    True,
        "has_reach":      False,
    },
    "TikTok": {
        "bronze_table": "improvado_analytics_bronze.tiktok_ads_landing",
        "platform_name": "TikTok",
        "has_video":      True,
        "has_quality":    False,
        "has_reach":      False,
    },
}
```

`SqlBuilder` methods (`build_quarantine_merge_sql()`,
`build_gold_merge_sql()`) iterate over this config and emit per-platform
SQL without code duplication. Adding a fourth platform is a single config
entry, not a code change.

### 8.10 _ensure_infrastructure()

Called in `__init__` before any BigQuery operation. Creates:
- `improvado_analytics_bronze` dataset (if not exists)
- `improvado_analytics_staging` dataset (if not exists)
- `improvado_analytics_production` dataset (if not exists)
- `tbl_ingestion_audit` table (if not exists)
- `stg_quarantine_logs` table (if not exists)

This makes the pipeline self-bootstrapping — a clean GCP project with no
pre-existing resources runs successfully on the first invocation.

### 8.11 AuditLogger (append-only, no DML UPDATE)

`AuditLogger` records every status transition as a new INSERT row, never
updating existing rows with DML. This preserves the full audit trail even
if a run crashes mid-execution.

```python
# Every status change inserts a new row
audit_logger.log(run_id, "STARTED",    platform="all")
audit_logger.log(run_id, "VALIDATING", platform="Facebook", rows=110)
audit_logger.log(run_id, "MERGING",    platform="Facebook")
audit_logger.log(run_id, "SUCCESS",    platform="Facebook", rows_merged=110)
```

Insert method uses `load_table_from_json` (not `client.insert_rows`) so the
audit write is a load job with its own `maximum_bytes_billed` guardrail.

### 8.12 SAFE_CAST Pattern

All type conversions from Bronze (all-STRING) to Gold (typed) use this pattern:

```python
COALESCE(SAFE_CAST(NULLIF({col}, '') AS {dtype}), 0)
```

- `NULLIF(col, '')` converts empty string to NULL
- `SAFE_CAST(... AS dtype)` returns NULL (not an error) on type mismatch
- `COALESCE(..., 0)` converts NULL to zero

This makes the pipeline tolerant of blank cells, extra whitespace, or
upstream type changes that slip past Bronze validation.

### 8.13 engagement_rate — Removed from Bronze, Computed in Gold

`engagement_rate` was present in the original v1.0 bronze reads as a raw
column. In v2.0 it was removed from L1 bronze reads and computed in L2 (gold)
as a derived column:

```sql
SAFE_DIVIDE(likes + shares + comments, impressions) AS engagement_rate
```

**Why:** The platforms each compute engagement rate differently (some include
saves, some include link clicks). Computing it consistently in gold from
first-principle numerators (likes, shares, comments, impressions) ensures
cross-platform comparability.

**Effect on G8 validation:** G8 (`engagement_rate BETWEEN 0.0 AND 1.0`)
was removed from the quarantine validation rules — you cannot quarantine a
computed column. See Section 9.4 note.

### 8.14 Cost Guardrails

| Parameter | Value | Where set |
|---|---|---|
| `GOLD_TXN_MAX_BYTES` | 3 GB | `QueryJobConfig` on the gold MERGE transaction |
| `BQ_LOCATION` | `"US"` | All dataset creation calls |
| `MAX_AUDIT_BYTES` | 10 MB | `LoadJobConfig` on audit inserts |

### 8.15 gold_unified_performance_mart.sql — 398 Lines, Documentation Only

`queries/gold_unified_performance_mart.sql` is a documentation artifact, not
the executable SQL. The executable SQL is generated at runtime by `SqlBuilder`
from `_PLATFORM_CONFIG`. The SQL file matches what SqlBuilder would emit and
serves as a human-readable reference.

**Structure of the file:**
- **Phase 1 (lines 1–80):** `CREATE TABLE IF NOT EXISTS` DDL with 30 columns,
  NOT NULL constraints, partition, cluster, OPTIONS description.
- **Phase 2 Stmt 1–3 (lines 82–280):** One quarantine MERGE per platform
  (`facebook_gold_validation`, `google_gold_validation`,
  `tiktok_gold_validation`). Each uses the SAFE_CAST pattern and routes
  invalid rows to `stg_quarantine_logs`.
- **Phase 2 Stmt 4 (lines 282–398):** `BEGIN TRANSACTION; MERGE×3; COMMIT`
  — three gold MERGEs (one per platform) wrapped in a single atomic
  transaction. If the TikTok MERGE fails after Facebook and Google have
  already merged, the entire transaction rolls back.

### 8.16 Verified Production Output

```
Script 02 — Gold Transformation  started
Parameters  first_run=False  platforms=3  gold_txn_max_bytes=3GB
Running 3 quarantine MERGEs …
  facebook_gold_validation: 0 rows quarantined
  google_gold_validation:   0 rows quarantined
  tiktok_gold_validation:   0 rows quarantined
Running gold MERGE transaction …
  BEGIN TRANSACTION
  Facebook MERGE: 110 rows inserted
  Google   MERGE: 110 rows inserted
  TikTok   MERGE: 110 rows inserted
  COMMIT TRANSACTION
Gold table: 330 rows  30 columns  24 NOT NULL
Financial reconciliation:
  Facebook:  bronze=$18,292.00  gold=$18,292.00  Δ=$0.00  ✓
  Google:    bronze=$37,686.20  gold=$37,686.20  Δ=$0.00  ✓
  TikTok:    bronze=$74,266.70  gold=$74,266.70  Δ=$0.00  ✓
  Total:     bronze=$130,244.90 gold=$130,244.90 Δ=$0.00  ✓
Script 02 COMPLETE
```

---

## 9. Data Validation Matrix (Bronze + Gold)

> Two-layer validation. Python validates schema (Bronze ingestion). SQL validates
> business rules (Gold transformation). Bad rows go to quarantine with structured
> reasons at both layers.

### 9.1 Validation Layer Responsibilities

| Layer | What it validates | Why here |
|---|---|---|
| **Bronze (Python)** | Type parsing, structural keys, basic non-negativity | Catches malformed files at ingestion before they hit BQ |
| **Gold (SQL)** | Business ranges, logical relationships, cross-column consistency | Final guardrail before data reaches dashboard |

Both write rejected rows to the same `stg_quarantine_logs` table, distinguished by
`origin_platform` field (`facebook` vs `facebook_gold_validation`).

### 9.2 Bronze Validation Rules (Python — extending v1.0)

Every Bronze row must pass all of these:

| # | Rule | Type | Action on fail |
|---|---|---|---|
| **R0** | `date` not null and `campaign_id` not null | Structural | Quarantine |
| **R1** | `date` parses as YYYY-MM-DD and ∈ [2020-01-01, today+1] | Date type | Quarantine |
| **R2a** | `spend`/`cost` parses as float | Numeric type | Quarantine |
| **R2b** | `spend`/`cost` >= 0 | Range | Quarantine |
| **R3a** | `impressions`, `clicks`, `conversions` parse as int | Numeric type | Quarantine |
| **R3b** | `impressions` >= `clicks` >= `conversions` >= 0 | Logical | Quarantine |
| **R4** | All integer columns parse as int (`video_views`, `reach`, `likes`, `shares`, `comments`, `video_watch_*`, `quality_score`) | Numeric type | Quarantine |
| **R5** | All float columns parse as float (`conversion_value`, `frequency`, `engagement_rate`, `search_impression_share`, `avg_cpc`) | Numeric type | Quarantine |
| **R6** | All dimension columns (`campaign_name`, `ad_set_id/name`, `ad_group_id/name`, `adgroup_id/name`) are non-empty strings | Structural | Quarantine |
| **R7** | Row dict is JSON-serializable for quarantine logging | Serialization | Quarantine |

### 9.3 Bronze Imputation Rules

For rows that **pass** validation:

| Column type | Null/empty value handling |
|---|---|
| Numeric (any of 21 numeric fields) | Substitute `"0"` (preserves all-string Bronze pattern) |
| Dimension (any string field) | Substitute `"UNKNOWN_DIMENSION"` |

### 9.4 Gold Validation Rules (SQL — NEW in v2.0)

The gold transformation CTE includes a validation pass. Rows failing any rule
are excluded from the gold MERGE and inserted into `stg_quarantine_logs` with
the violated rule identifier.

| # | Rule | Column | Constraint | Action |
|---|---|---|---|---|
| **G1** | `spend >= 0.0` | spend | Range | Reject + quarantine |
| **G2** | `impressions >= 0` | impressions | Range | Reject + quarantine |
| **G3** | `clicks >= 0` AND `clicks <= impressions` | clicks | Logical | Reject + quarantine |
| **G4** | `conversions >= 0` AND `conversions <= clicks` | conversions | Logical | Reject + quarantine |
| **G5** | `conversion_value >= 0` | conversion_value | Range | Reject + quarantine |
| **G6** | `quality_score = 0 OR quality_score BETWEEN 1 AND 10` | quality_score | Range (Google only) | Reject + quarantine |
| **G7** | `frequency = 0 OR frequency >= 1.0` | frequency | Range (Facebook only, "avg times per user" >= 1) | Reject + quarantine |
| ~~**G8**~~ | ~~`engagement_rate BETWEEN 0.0 AND 1.0`~~ | ~~engagement_rate~~ | ~~Range~~ | **REMOVED** — `engagement_rate` is a computed gold column (`SAFE_DIVIDE(likes+shares+comments, impressions)`), not a raw bronze column. Cannot be quarantined at ingestion. Renumber remaining rules if desired. |
| **G9** | `search_impression_share BETWEEN 0.0 AND 1.0` | search_impression_share | Range (Google only) | Reject + quarantine |
| **G10** | `reach <= impressions` | reach | Logical (Facebook only — reach is unique users, ≤ total impressions) | Reject + quarantine |
| **G11** | All `video_watch_25 >= video_watch_50 >= video_watch_75 >= video_watch_100` | TikTok funnel | Logical (funnel monotonicity) | Reject + quarantine |
| **G12** | `video_watch_25 <= video_views` (TikTok only) | TikTok | Logical | Reject + quarantine |

### 9.5 SQL Implementation Pattern

```sql
WITH fb_validated AS (
    SELECT 
        *,
        -- Build a STRUCT of failed checks; row is invalid if any check fails
        ARRAY(
            SELECT rule FROM UNNEST([
                IF(spend < 0,                                          'G1_NEGATIVE_SPEND', NULL),
                IF(impressions < 0,                                    'G2_NEGATIVE_IMPRESSIONS', NULL),
                IF(clicks < 0 OR clicks > impressions,                 'G3_INVALID_CLICKS', NULL),
                IF(conversions < 0 OR conversions > clicks,            'G4_INVALID_CONVERSIONS', NULL),
                IF(conversion_value < 0,                               'G5_NEGATIVE_REVENUE', NULL),
                IF(engagement_rate < 0 OR engagement_rate > 1,         'G8_BAD_ENGAGEMENT_RATE', NULL),
                IF(reach > impressions,                                'G10_REACH_EXCEEDS_IMPRESSIONS', NULL)
            ]) AS rule
            WHERE rule IS NOT NULL
        ) AS validation_failures
    FROM fb_processed
),
fb_passed AS (
    SELECT * EXCEPT(validation_failures)
    FROM fb_validated
    WHERE ARRAY_LENGTH(validation_failures) = 0
),
fb_rejected AS (
    SELECT 
        GENERATE_UUID() AS quarantine_id,
        CURRENT_TIMESTAMP() AS execution_timestamp,
        'facebook_gold_validation' AS origin_platform,
        TO_JSON_STRING(STRUCT(date, campaign_id, sub_group_id, spend, impressions, 
                              clicks, conversions, conversion_value)) AS raw_record_json,
        ARRAY_TO_STRING(validation_failures, '|') AS violated_rule_identifier,
        'PENDING' AS remediation_status
    FROM fb_validated
    WHERE ARRAY_LENGTH(validation_failures) > 0
);

-- Insert rejected rows into quarantine
INSERT INTO `improvado_analytics_staging.stg_quarantine_logs`
SELECT * FROM fb_rejected;

-- ... same pattern for Google and TikTok
-- Then MERGE the passed rows into gold
```

### 9.6 Why Two-Layer Validation

- **Bronze (Python):** Catches malformed data. Cheap (in-memory). Specific error messages per cell.
- **Gold (SQL):** Catches business-impossible data. Even if Bronze accepted it as "type-parseable", Gold confirms it's "business-meaningful". Example: `engagement_rate = 1.5` parses fine as a float but is impossible (>100%).

This catches edge cases like:
- Platform export bugs (engagement_rate accidentally reported as percentage instead of proportion)
- Restated data that was computed wrong (negative conversion_value)
- Tracking errors (clicks > impressions, which happens when click attribution windows close late)

---

## 10. Operational Layer

### 10.1 File Archival

After successful ingestion, the Cloud Function (or CLI invocation) moves the
processed file:

```python
# After successful MERGE
from google.cloud import storage
storage_client = storage.Client()
src_blob = storage_client.bucket(GCS_BUCKET).blob(f"incoming/{filename}")
dest_path = f"archive/{year}/{month:02d}/{filename}"
storage_client.bucket(GCS_BUCKET).rename_blob(src_blob, dest_path)
```

On failure:

```python
# On any pipeline error
src_blob = storage_client.bucket(GCS_BUCKET).blob(f"incoming/{filename}")
dest_path = f"failed/{filename}"
new_blob = storage_client.bucket(GCS_BUCKET).copy_blob(src_blob, GCS_BUCKET, dest_path)
new_blob.metadata = {
    "error_stage": error_stage,        # VALIDATION / MERGE / TRANSFORM
    "error_message": str(exc)[:1000],  # truncate for metadata size limits
    "failed_at": datetime.now(UTC).isoformat(),
}
new_blob.patch()
src_blob.delete()
```

### 10.2 Ingestion Audit Logging (status-transition pattern)

Per Section 7.3 — every script run owns one `audit_id` (UUID) and updates the
row at every status transition. Schema is partition-expired after 90 days.

### 10.3 Cost Guardrails

Every BigQuery query and load job sets `maximum_bytes_billed`:

| Operation | Cap |
|---|---|
| Bronze load (single CSV) | 100 MB |
| Bronze MERGE | 500 MB |
| Gold MERGE | 1 GB |
| Tier 2 read queries | 5 GB (lookback windows shouldn't exceed this) |
| Audit and quarantine writes | 50 MB |

If a job would exceed the cap, BigQuery rejects it before billing — fail-loud
rather than silent overspending.

### 10.4 Partition Expiration

| Table | Partition Expiration |
|---|---|
| `tbl_ingestion_audit` | 90 days |
| `stg_quarantine_logs` | 90 days |
| Bronze landing tables | None (preserve full history) |
| Gold mart | None (preserve full history) |
| Tier 2 outputs | N/A — single-version tables |

### 10.5 Schema Evolution Policy

- **Bronze tables:** Permissive. `ALLOW_FIELD_ADDITION` enabled on load jobs.
  New columns in upstream CSV auto-extend the bronze schema. Existing columns
  remain stable.
- **Gold mart:** Strict. Exactly 30 columns. Adding a column requires:
  1. Update Master LLD
  2. Update gold transformation SQL
  3. Re-run gold MERGE (incrementally backfills new column for old rows)
  4. Update dashboard widgets to reference new column
- **Tier 2 outputs:** Strict. Each script has explicit `_OUTPUT_SCHEMA`.

### 10.6 Dead Letter Queue Pattern (documented, not deployed)

For production deployment of the Cloud Function:

```bash
# Create dead letter topic
gcloud pubsub topics create eventarc-deadletter

# Configure function with dead letter on retry exhaustion
gcloud functions deploy validate_and_ingest \
    --gen2 \
    --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
    --trigger-event-filters="bucket=improvado-analytics-lakehouse-raw" \
    --retry \
    --max-retry-attempts=5 \
    --dead-letter-topic=eventarc-deadletter
```

A separate subscriber on `eventarc-deadletter` sends an alert (email, Slack)
when an event fails after 5 retries.

### 10.7 Retry Strategy

| Component | Retry policy |
|---|---|
| BigQuery operations (within scripts) | 3 attempts, exponential backoff (2s, 4s, 8s) |
| LLM API calls | 3 attempts, exponential backoff |
| HTTP timeouts | 45-second timeout per attempt |
| Cloud Function invocation (Eventarc) | Up to 5 retries, exponential backoff (handled by GCP) |
| GCS file operations | 3 attempts with backoff |

---

## 11. Tier 2 — Step 3: Anomaly Detection (90-day Window)

### 11.1 Algorithm

Rolling 7-day Z-score per (platform, campaign_id) group on observed CPA. Cold-start
fallback to global campaign stats if < 3 days history. Bidirectional flagging
(HIGH_CPA / LOW_CPA / NORMAL).

> **Algorithmic correctness:** Uses `groupby().transform()` to ensure rolling
> windows are computed strictly within each (platform, campaign_id) group.
> A naive `.shift().rolling()` chain would contaminate adjacent groups (proven
> bug, fixed in this version).

### 11.2 Lookback Window

`LOOKBACK_WINDOW_DAYS = 90` — anchored to the most recent date in gold (not
`CURRENT_DATE`). Adapts automatically to future data loads.

### 11.3 Output Schema

`improvado_analytics_staging.fct_anomaly_flags` (WRITE_TRUNCATE):

| Column | Type | Description |
|---|---|---|
| `date` | DATE | Record date |
| `platform` | STRING | Facebook / Google / TikTok |
| `campaign_id` | STRING | Campaign identifier |
| `campaign_name` | STRING | Display name |
| `observed_cpa` | FLOAT64 | CPA for the date |
| `rolling_mean_cpa` | FLOAT64 | 7-day rolling mean |
| `rolling_std_cpa` | FLOAT64 | 7-day rolling std |
| `z_score` | FLOAT64 | Standardized deviation |
| `is_anomaly` | INT64 | 1 = anomalous, 0 = normal |
| `anomaly_direction` | STRING | HIGH_CPA / LOW_CPA / NORMAL |

### 11.4 Execution

```bash
python pipelines/03_anomaly_detection.py
```

---

## 12. Tier 2 — Step 4: Budget Optimizer (30-day Window)

### 12.1 Algorithm

`scipy.optimize.linprog` (HiGHS solver). Maximises `sum(budget_i / CPA_i)` subject
to:
- Sum of budgets = total budget
- Each platform >= 10% of total (multi-channel presence floor)
- Each platform <= 2x current spend (avoids unrealistic step-changes)

### 12.2 Lookback Window

`LOOKBACK_WINDOW_DAYS = 30` — uses most recent 30 days of efficiency data.

### 12.3 Output Schema

`improvado_analytics_staging.tbl_budget_recommendations` (WRITE_TRUNCATE):

13 columns including `generated_at`, `analysis_period_start/end`, current/recommended
spend + percentages, projected conversions, conversion delta, assumption note.

### 12.4 Critical Implementation Note

Platform-coefficient alignment is **explicit** via DataFrame sort + index-based
mapping, never relying on BigQuery `GROUP BY` row order. Without this, the
solver result is silently misassigned (verified bug in v1.0 attempts).

### 12.5 Expected Result (Jan 2024 sample data)

| Platform | Current % | Recommended % | Projected Δ |
|---|---|---|---|
| Facebook | 14% | 28% | +2,395 |
| Google | 29% | 58% | +4,220 |
| TikTok | 57% | 14% | -5,088 |
| **Total** | 100% | 100% | **+1,526 (+11.4%)** |

### 12.6 Execution

```bash
python pipelines/04_budget_optimizer.py
```

### 12.7 Bug Fixes Applied Before Verified Run

Four bugs were identified and fixed before the live run shown in
Section 12.5:

1. **TOTAL row removed from BQ write.** The TOTAL summary row (sum across
   platforms) was being written to `tbl_budget_recommendations` as a 4th
   row. This breaks any downstream `SUM()` aggregation. TOTAL is now
   printed to terminal log only. The table always has exactly 3 rows.
   Verified: `Write OK  rows=3`.

2. **Zero-spend guard added.** If a platform had zero spend in the lookback
   window, HiGHS received a zero-CPA coefficient and returned an
   indeterminate solution. Fixed with `HAVING SUM(spend) > 0` in the SQL
   aggregation and a `ValueError` in `_validate_input()` if any platform
   reaches the solver with zero spend.

3. **`setup_logging` moved to first line of `main()`** before pipeline
   instantiation. Previously if `__init__` raised an exception, there was
   no logger to capture it.

4. **`%%` → `%` in uplift f-string.** Inside an f-string, `%%` prints as
   literal `%%`, not `%`. Fixed to single `%`.

---

## 13. Tier 2 — Step 5: Forecast (60-day Window)

### 13.1 Algorithm Selection: Holt-Winters with SES Fallback

**Primary: Holt-Winters Exponential Smoothing**
- `trend='add'`, `seasonal='add'`, `seasonal_periods=7` (weekly seasonality)
- Captures Mon-Sun day-of-week patterns specific to marketing data
- Requires >= 14 days of data (2 full seasonal cycles) to fit reliably

**Fallback: Simple Exponential Smoothing**
- Triggered when Holt-Winters fails to converge or insufficient data
- No seasonality assumption — just trend tracking
- Robust at any data length

### 13.2 Why Not Prophet

Prophet was considered (v1.0 used it). For 30-day to 60-day windows, Prophet
overfits — its multiple seasonality + changepoint model has more parameters than
training points. Holt-Winters is more stable for this scale.

### 13.3 Lookback Window

`LOOKBACK_WINDOW_DAYS = 60` — gives Holt-Winters at least 8 weekly cycles to fit.

### 13.4 Forecast Horizon

`FORECAST_HORIZON_DAYS = 14` — 2-week forward projection per platform per metric
(spend and conversions).

### 13.5 Output Schema

`improvado_analytics_staging.tbl_forecast` (WRITE_TRUNCATE):

| Column | Type | Description |
|---|---|---|
| `forecast_execution_date` | DATE | When the forecast was computed |
| `target_date` | DATE | Future date being predicted |
| `platform` | STRING | Facebook / Google / TikTok |
| `metric_name` | STRING | spend / conversions |
| `predicted_value` | FLOAT64 | Point prediction |
| `lower_bound` | FLOAT64 | 80% CI lower |
| `upper_bound` | FLOAT64 | 80% CI upper |
| `model_used` | STRING | HOLT_WINTERS / SES_FALLBACK |

### 13.6 Execution

```bash
python pipelines/05_forecast.py
```

### 13.7 Implementation Notes

**Initialization waterfall — two HW attempts before SES:**
```python
for init_method in ("estimated", "heuristic"):
    try:
        model = ExponentialSmoothing(
            series, trend="add", seasonal="add",
            seasonal_periods=7, initialization_method=init_method,
        )
        fit = model.fit(optimized=True)
        if not np.isnan(fit.forecast(horizon).values).any():
            return fit  # success
    except Exception:
        continue
# Both failed → SES
```
`initialization_method='estimated'` uses ML estimation of initial states
(preferred). `'heuristic'` uses linear regression on the first two seasonal
cycles. SES is only triggered if both HW attempts raise or return NaN.

**Confidence interval formula (80%):**
```
margin_h = z₈₀ × σ × √h
```
- `σ` = `std(in-sample residuals, ddof=1)` from the fitted model
- `z₈₀ = 1.2816`
- `h` = horizon step (1 … 14)
- Edge case: if `σ < 1e-9` (near-constant series), substitute
  `max(mean(|forecast|) × 0.05, 1e-6)` to avoid zero-width intervals.

Both `predicted_value` and `lower_bound` are clipped to `max(value, 0.0)`.

**Missing-date handling:**
Platform series are reindexed to a complete daily `pd.date_range` after the
BQ query. Gaps filled with `fill_value=0.0`. Ensures HW/SES always receives
a regular time series without holes.

**Data scale note:**
`LOOKBACK_DAYS = 60` per spec. With only 30 days in the gold table, the
query returns all 30 days (Jan 1–30). `30 ≥ 14` (2 × seasonal_periods=7),
so HW succeeds — all 6 series used HOLT_WINTERS on the verified run.

### 13.8 Runtime Fixes Applied After First Failing Run

Three bugs surfaced when the script was first executed and were fixed before
the verified run:

1. **`statsmodels` moved to module-level import with helpful error.**
   The imports were lazy (inside the fitting methods). When `statsmodels`
   was missing from the venv, the `ModuleNotFoundError` only appeared
   mid-execution after BQ had already run. Fixed:
   ```python
   try:
       from statsmodels.tsa.holtwinters import ExponentialSmoothing, SimpleExpSmoothing
   except ImportError as e:
       raise ImportError(
           "statsmodels is required. Install: pip install statsmodels>=0.14.0"
       ) from e
   ```
   Now fails immediately on startup with a clear install instruction.

2. **BigQuery Storage `UserWarning` suppressed.**
   `google-cloud-bigquery` emits
   `"BigQuery Storage module not found, fetch data with the REST endpoint instead."`
   when `google-cloud-bigquery-storage` is not installed. Benign (REST is
   adequate for 330 rows) but clutters the log. Added at module level:
   ```python
   warnings.filterwarnings(
       "ignore",
       message="BigQuery Storage module not found",
       category=UserWarning,
       module="google.cloud.bigquery.table",
   )
   ```

3. **Date display truncated to `[:10]`.**
   `str(pd.Timestamp('2024-01-01'))` produces `'2024-01-01 00:00:00'`.
   Log line showed `"period: 2024-01-01 00:00:00 → 2024-01-30 00:00:00"`.
   Fixed: `str(daily_df["date"].min())[:10]` and same for `.max()`.

### 13.9 Verified Live Run Output

```
2026-06-11T07:44:45 INFO  Script 05 — Forecast  started
2026-06-11T07:44:46 INFO  Parameters  lookback=60 d  horizon=14 d
                          seasonal_periods=7  confidence=80%  model=HW+SES_fallback
2026-06-11T07:44:46 INFO  Fetched daily aggregates
                          period: 2024-01-01 → 2024-01-30  (30 days)
2026-06-11T07:44:46 INFO  Forecasting  Facebook   spend         obs=30  non-zero=30
2026-06-11T07:44:47 INFO  Forecasting  Facebook   conversions   obs=30  non-zero=30
2026-06-11T07:44:47 INFO  Forecasting  Google     spend         obs=30  non-zero=30
2026-06-11T07:44:47 INFO  Forecasting  Google     conversions   obs=30  non-zero=30
2026-06-11T07:44:47 INFO  Forecasting  TikTok     spend         obs=30  non-zero=30
2026-06-11T07:44:47 INFO  Forecasting  TikTok     conversions   obs=30  non-zero=30
2026-06-11T07:44:48 INFO  Forecast window: 2024-01-31 → 2024-02-13
2026-06-11T07:44:48 INFO    Platform   Metric       Model            14d Total   Daily Avg
2026-06-11T07:44:48 INFO    Facebook   conversions  HOLT_WINTERS      1,496.64      106.90
2026-06-11T07:44:48 INFO    Facebook   spend        HOLT_WINTERS   $10,914.05   $   779.58
2026-06-11T07:44:48 INFO    Google     conversions  HOLT_WINTERS      2,480.99      177.21
2026-06-11T07:44:48 INFO    Google     spend        HOLT_WINTERS   $22,679.81   $ 1,619.99
2026-06-11T07:44:48 INFO    TikTok     conversions  HOLT_WINTERS      4,478.00      319.86
2026-06-11T07:44:48 INFO    TikTok     spend        HOLT_WINTERS   $47,953.70   $ 3,425.26
2026-06-11T07:44:48 INFO  Models used: HOLT_WINTERS=6  SES_FALLBACK=0  (out of 6 series)
2026-06-11T07:44:50 INFO  Write OK  attempt=1  rows=84
2026-06-11T07:44:50 INFO  Script 05 COMPLETE  rows_written=84
                          table=improvado-analytics-lakehouse
                               .improvado_analytics_staging.tbl_forecast
```

84 rows = 14 horizon × 3 platforms × 2 metrics. All 30 observations are
non-zero → HW succeeded on all 6 series, SES fallback not triggered.

---

## 14. Tier 2 — Step 6: GenAI Executive Summary (Model-Agnostic)

> **⚠️ SUPERSEDED IN THE STREAMLIT BUILD.** The standalone
> `pipelines/06_llm_executive_summary.py` script described below — which
> wrote a single summary row to `tbl_ai_executive_summary` — has been
> **absorbed into `streamlit_app/lib/insight_generator.py`**, which generates
> FIVE insight cards on demand (not one batch row), each independently
> falling back to rule-based text if the LLM is unavailable. The REST-based,
> model-agnostic LLM architecture described in 14.1 (env vars, OpenAI-
> compatible endpoint) is preserved and extended — see Section 17.6 for the
> full resilience layer (429/413 handling, model fallback). The prompt
> construction (14.2) and error-handling philosophy (14.3, rule-based
> fallback) carry over conceptually to each of the five cards. The original
> spec below is retained for historical reference; `06_llm_executive_summary.py`
> can be deleted or kept as a deprecated standalone script (decision pending
> — see PROJECT_STATUS_REPORT.md Section 3.4).

### 14.1 Architecture

REST-based, OpenAI-compatible. Works with any of:
- Groq (free tier, fast) — `https://api.groq.com/openai/v1/chat/completions`
- OpenRouter (free models) — `https://openrouter.ai/api/v1/chat/completions`
- Google Gemini (via OpenAI-compatible endpoint)
- OpenAI direct
- Local Ollama / LM Studio

All configured via three env vars:
- `LLM_ENDPOINT_URL`
- `LLM_BEARER_TOKEN`
- `LLM_TARGET_MODEL`

### 14.2 Prompt Construction

Context packet (built from live BQ queries):

```json
{
  "execution_date": "2024-02-15",
  "evaluation_period": "Last 30 days in gold table",
  "total_spend_usd": 130244.90,
  "total_conversions": 13363,
  "blended_cpa_usd": 9.75,
  "anomalies_detected": 12,
  "platform_breakdown": {
    "Facebook": {"spend_usd": 18292.0, "spend_pct": 14.0, "cpa": 7.64, ...},
    ...
  }
}
```

Prompt rules:
- Exactly 3 sentences
- Sentence 1: best-performing platform with specific CPA
- Sentence 2: highest-risk pattern with quantitative comparison
- Sentence 3: specific reallocation recommendation with projected uplift
- No preamble, no markdown, no bullet points

### 14.3 Error Handling

- HTTP retry: 3 attempts with exponential backoff
- Timeout: 45 seconds per attempt
- **Rule-based fallback** if all attempts fail — generates a deterministic
  summary from the context packet using simple string templating. Never returns
  empty.

### 14.4 Output Schema (as built before superseded by Streamlit)

`improvado_analytics_staging.tbl_ai_executive_summary` (WRITE_TRUNCATE,
**5 rows** — one per card type):

| Column | Type | Description |
|---|---|---|
| `generated_at` | TIMESTAMP | When the card was generated |
| `card_type` | STRING | EXECUTIVE\_SUMMARY / WORST\_PERFORMER / BUDGET\_OPTIMIZATION / FORECAST\_OUTLOOK / ANOMALY\_NARRATIVE |
| `card_title` | STRING | Display title for the dashboard card |
| `summary_text` | STRING | LLM-generated or rule-based narrative |
| `context_json` | STRING | Full JSON context packet used for generation (audit trail) |
| `model_used` | STRING | LLM model name or `"RULE_BASED_FALLBACK"` |

> **⚠️ SUPERSEDED.** This schema and the standalone
> `pipelines/06_llm_executive_summary.py` are superseded by
> `streamlit_app/lib/insight_generator.py`, which generates the same 5
> cards on demand with `@st.cache_data(ttl=600)` — no BQ write required.
> The `tbl_ai_executive_summary` table can be dropped:
> ```bash
> bq rm -f improvado-analytics-lakehouse:improvado_analytics_staging.tbl_ai_executive_summary
> ```

### 14.5 Execution

```bash
export LLM_BEARER_TOKEN="your_key"
python pipelines/06_llm_executive_summary.py
```

---

## 15. Step 7: QA Validation Runner

### 15.1 Adapted for Incremental Reality

v1.0 hardcoded 330 rows. v2.0 derives expected counts dynamically from bronze:

```sql
-- Expected gold row count = sum of bronze row counts
SELECT 
    (SELECT COUNT(*) FROM facebook_ads_landing) +
    (SELECT COUNT(*) FROM google_ads_landing) +
    (SELECT COUNT(*) FROM tiktok_ads_landing) AS expected_gold_rows
```

### 15.2 Checks Performed

| Check | Method | Severity |
|---|---|---|
| Gold row count = sum of bronze counts | Dynamic SQL | Critical |
| Gold column count = 30 | Hardcoded | Critical (Data Contract) |
| Per-platform spend reconciliation (bronze == gold, within $0.01) | SQL | Critical |
| Per-platform conversion reconciliation | SQL | Critical |
| Null dates in gold | SQL | Critical |
| Negative spend in gold | SQL | Critical |
| ML output tables exist and non-empty | SQL | Warning |
| Most recent ingestion audit status = SUCCESS | SQL | Warning |
| Quarantine count for current run | SQL | Informational |

### 15.3 Execution

```bash
python pipelines/07_qa_validation.py
# Exit 0 = pass, exit 1 = critical failure
```

---

## 16. Step 8: Cloud Function Deployment (Eventarc Trigger)

### 16.1 Cloud Function Entry Point (`cloud_function/main.py`)

```python
"""
Cloud Function 2nd gen entry point for GCS object.finalized events.
Receives CloudEvent, extracts file path, invokes ingestion pipeline.
"""

import functions_framework
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pipelines import validate_and_ingest_01 as ingest_module


@functions_framework.cloud_event
def handle_storage_event(cloud_event):
    """Triggered by google.cloud.storage.object.v1.finalized event."""
    data = cloud_event.data
    bucket = data["bucket"]
    name = data["name"]

    # Skip files in archive/ or failed/ directories
    if not name.startswith("incoming/"):
        return f"Ignored: {name} not in incoming/"

    # Construct full GCS URI
    gcs_uri = f"gs://{bucket}/{name}"

    # Invoke ingestion pipeline
    pipeline = ingest_module.IngestionPipeline.from_gcs_path(gcs_uri)
    result = pipeline.run()

    if not result.success:
        # File auto-moves to failed/ via pipeline's archival logic
        raise RuntimeError(f"Ingestion failed for {gcs_uri}: {result.errors}")

    return f"Successfully ingested {gcs_uri}"
```

### 16.2 Deployment Command (`cloud_function/cloudbuild.yaml`)

```yaml
steps:
  - name: 'gcr.io/google.com/cloudsdktool/cloud-sdk'
    entrypoint: 'bash'
    args:
      - '-c'
      - |
        gcloud functions deploy validate_and_ingest \
            --gen2 \
            --runtime=python311 \
            --region=us-east4 \
            --source=. \
            --entry-point=handle_storage_event \
            --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
            --trigger-event-filters="bucket=improvado-analytics-lakehouse-raw" \
            --memory=1024MB \
            --timeout=540s \
            --max-instances=10 \
            --service-account=ingestion-pipeline@${PROJECT_ID}.iam.gserviceaccount.com \
            --set-secrets=LLM_BEARER_TOKEN=llm-api-key:latest
```

### 16.3 Required IAM Permissions

Service account `ingestion-pipeline@<project>.iam.gserviceaccount.com` needs:

| Role | Resource | Why |
|---|---|---|
| `roles/storage.objectAdmin` | GCS bucket | Read incoming/, move to archive/failed/ |
| `roles/bigquery.dataEditor` | BQ project | Write to all datasets |
| `roles/bigquery.jobUser` | BQ project | Run queries and load jobs |
| `roles/secretmanager.secretAccessor` | Secret Manager | Read LLM API key |
| `roles/eventarc.eventReceiver` | Project | Receive Eventarc events |

### 16.4 Assignment-Mode Operation

For assignment, the function is **not deployed** to keep costs at zero. The
pipeline runs via CLI. The deployment code lives in `cloud_function/` as
evidence of production-grade design.

To demonstrate trigger pattern during the video walkthrough:
```bash
# Show the deployment config without actually deploying
cat cloud_function/main.py
cat cloud_function/cloudbuild.yaml
```

---

## 17. Step 9: Dashboard Specification (Streamlit — As Built)

> **This section supersedes the original v1.0/v2.0 Looker Studio
> specification.** The presentation layer was built as a multi-page
> Streamlit application (`streamlit_app/`) rather than Looker Studio. Every
> capability the original spec called for — and several it didn't — is
> implemented and tested. This section documents the architecture AS BUILT,
> including every refinement made during the dashboard sprint
> (Sessions 1–7 of development).
>
> For a fix-by-fix changelog of bugs found and resolved during development,
> see `PROJECT_STATUS_REPORT.md`.

### 17.1 Why Streamlit Instead of Looker Studio

| Looker Studio | Streamlit (chosen) |
|---|---|
| Free connector to BigQuery, drag-and-drop charts | Free, runs anywhere (Community Cloud, Cloud Run, local) |
| No code — limited to Looker's chart types and calculated fields | Full Python — custom Plotly figures, arbitrary layout, custom CSS |
| No conversational interface | Native chat UI (`st.chat_input`, `st.chat_message`) — enables the AI chat requirement |
| Static once published; refresh on schedule | Recomputes from BigQuery on every page load (5-min cache) — always current |
| Cannot easily embed an LLM-backed chatbot | LLM calls are just Python `requests` — directly embeddable |

The chat-with-your-data requirement (LLD original scope) is fundamentally a
conversational interface, which Looker Studio cannot provide without an
external embed. Streamlit was chosen because the SAME tool serves both the
static-dashboard requirement AND the conversational requirement, with one
codebase, one deployment, one BigQuery client.

### 17.2 Page Architecture (5 pages)

```
streamlit_app/
├── Home.py                              — Executive landing page
├── install_deps.py                      — Dependency installer (--check/--upgrade/--add/--venv)
├── requirements.txt                     — Exact-pinned versions
├── lib/                                  — Service layer (16 modules, Section 17.3)
└── pages/
    ├── 1_📊_Performance_Overview.py     — KPI strip, daily trend, platform table, scatter, radar, conversion quality
    ├── 2_💡_AI_Insights.py              — 5 AI-generated insight cards
    ├── 3_🎯_Channel_Deep_Dives.py       — Per-platform tabs (Facebook / Google / TikTok)
    └── 4_💬_Chat_With_Your_Data.py      — Chain-of-thought conversational analytics
```

| Page | Purpose | Key widgets |
|---|---|---|
| **Home** | At-a-glance executive summary, no hardcoded numbers | Headline strip computed from live KPIs; platform performance cards with full-form labels ("Cost per Acquisition", "Click-Through Rate") and responsive `repeat(auto-fit, minmax(90px,1fr))` grid; spend-allocation donut with **external HTML legend** (rendered as flex-row markdown below the chart — Plotly's internal legend was overlapping the donut on narrow viewports); top-opportunity + most-severe-anomaly callouts |
| **Performance Overview** | Operational detail + diagnostic view of the funnel | KPI strip with **period-over-period delta tooltips** that explicitly explain the comparison window (recent half vs earlier half of the date range, with computed `Jan 16 – Jan 30 vs Jan 01 – Jan 15` substrings); daily spend trend (stacked area); platform performance table; **campaign efficiency scatter** (spend vs CPA) with amber median lines (`#FBBF24`, dashed, 1.5px, with dark-pill annotation backgrounds — replaced the earlier dim-gray dotted lines that blended into the data); best/worst campaign callouts directly below the scatter; **Platform Strength Radar** (5-dimension normalized 0–10 spider chart) + **Conversion Quality** chart (grouped CTR% vs CVR% per platform, plain-English diagnostic caption auto-generated below) as 50/50 side-by-side bordered cards |
| **AI Insights** | The "GenAI" requirement, made interactive | 5 cards — Worst Performer Alert, Budget Reallocation, 14-Day Forecast, Anomaly Patterns, plus a synthesis card — each labeled `AI · <model>` or showing the rule-based fallback badge; **anomaly chart with 7-day warm-up shading** (amber `add_vrect`, inline annotation "warm-up · no anomaly detection") and **z ≥ 2.5 severity filter** for display; "🔬 How are anomalies detected?" expander documenting the algorithm's honest limitations (lag bias, false-positive expectation, normality assumption) and production upgrade path (MAD-based modified Z-score, minimum-history gate, day-of-week baselines) |
| **Channel Deep Dives** | Platform-specific detail | Tabs per platform with `padding: 8px 4px 12px 4px` inner-div wrappers (fixed the missing-bottom-padding issue); **Google tab** adds three health cards — Quality Score (formatted `7.7 / 10`), Search Impression Share (`60.7 %`), Return on Ad Spend (`5.60×`) — each with their own padded inner div; **Facebook tab** adds Audience Saturation (Reach, Frequency) with a status pill and Frequency-vs-CPA scatter; **ROAS chart** with break-even line at `x=1.0` rendered as **2px amber dashed** with a bordered dark-pill annotation "**Break-even (1.0×)**" positioned at chart bottom (was previously a thin gray line with a top-cut-off label) |
| **Chat With Your Data** | Conversational analytics — Section 17.5 | Chain-of-thought reasoning trace (expandable steps with ok/pending/error status); SQL transparency (expandable); **inline chart rendering** for "chart that" follow-ups; data table preview; per-message `key=f"chat_chart_{msg_key}"` on `st.plotly_chart` (fixes the "multiple plotly_chart elements with same auto-generated ID" crash on history replay) |

Every page also embeds a **page-aware sidebar chatbot** AND a **floating AI
beacon** in the main content area (`lib/page_chatbot.py`) — see Section 17.5.4.

### 17.3 Service Layer (`lib/`)

| Module | Responsibility |
|---|---|
| `config.py` | Environment/secrets loading; BQ table refs (`GOLD_REF`, `ANOMALY_REF`, `BUDGET_REF`, `FORECAST_REF`); per-context byte budgets (`MAX_BYTES_CHAT=50MB`, `MAX_BYTES_OVERVIEW=500MB`, `MAX_BYTES_INSIGHTS=200MB`); `PLATFORM_COLORS` |
| `bq_client.py` | Cached + uncached BigQuery query execution; `BQUserFriendlyError` translates raw BQ exceptions (bytes-billed, permission, syntax, timeout, rate-limit) into safe user-facing messages while logging the technical detail; **`discover_table_schema()`** + **`discover_schemas_from_sql()`** — query `INFORMATION_SCHEMA.COLUMNS` for the live column list of any referenced table (used by self-healing SQL, Section 17.4) |
| `llm_client.py` | OpenAI-compatible REST client; **429 retry-after parsing** (handles Groq's `"Please try again in X.Xs"` body format, including `"33m54.72s"` minute+second format); **TPM-exhaustion → fallback-model switch**; **TPD (tokens-per-day) → no retry, surface `daily_limit_message` to user with parsed wait time**; **413 payload-too-large → progressive shrink-and-retry** capped independently at 6 cycles (Section 17.6) |
| `sql_agent.py` | Schema-grounded NL→SQL generation, with conversation history for follow-ups ("now show TikTok") |
| `sql_validator.py` | **9-layer defense** (Section 17.4) |
| `scope_guard.py` | Permissive **blocklist** (catches cooking/weather/code/poems/translation) — rejected the original allowlist design because it false-rejected legitimate dashboard questions like "what is this page about?" |
| `cot_chat.py` | Chain-of-thought orchestration: intent classification → SQL generation/planning → validation → self-healing execution → narration. Houses the **OUT_OF_SCOPE pre-validation check**, **visualize-intent routing**, **DML false-positive fallback**, and **narrative sanitizer** (Section 17.5) |
| `chart_builder.py` | **Constrained chart-spec builder** — the LLM picks `chart_type` (5-element enum) + column names (validated against the live DataFrame); five pre-written Plotly functions render the result. No code generation, no `exec()` (Section 17.7) |
| `page_chatbot.py` | Sidebar chatbot embedded on every page + **floating AI beacon** in the main content area (Section 17.5.4). Injects page context as a synthetic prior turn, delegates to the full `cot_chat` pipeline, renders iMessage-style bubbles with animated "thinking" loading state |
| `insight_generator.py` | The 5 AI Insights cards — each independently calls the LLM with a card-specific prompt and falls back to a deterministic rule-based summary if the LLM is unavailable or errors |
| `prompts.py` | All system prompts: intent classifier (5 intents), SQL agent (11 hard rules incl. column-scoping with explicit GROUP-BY violation example), data planner (with hardcoded analytical fallback example for broad-insight questions), chat narrator, suggestion synthesizer, chart-spec selector, page-context assistant |
| `schema_registry.py` | Single source of truth for the 30-column gold-mart schema, shared by the SQL agent's prompt and the validator's column checks |
| `formatters.py` | Compact numeric formatting ($130.2K, 13.4K, 1.70%) |
| `glossary.py` | Maps abbreviations to full forms ("CPA" → "Cost per Acquisition") for both display labels and tooltip help text |
| `page_style.py` | Shared CSS injected on every page: equal-height bordered cards in columns (`div[data-testid="column"] > div > [data-testid="stVerticalBlockBorderWrapper"] { height: 100% }`), KPI metric font sizing (30px values, 12px labels), sidebar styling, scrollbar theming |

### 17.4 Safety & Validation Stack

Every SQL statement the LLM generates — whether from `sql_agent.py` (factual
questions) or the data planner (suggestion/analytical questions) — passes
through `sql_validator.validate()` before touching BigQuery:

1. Length cap on the raw SQL string
2. Strip SQL comments before keyword scanning (prevents `/* DROP TABLE */`-style hiding)
3. Forbidden-keyword check: `INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, MERGE, TRUNCATE, GRANT, REVOKE, EXEC, CALL, COPY, LOAD, DECLARE, BEGIN, COMMIT`
4. Suspicious-pattern check (e.g. stacked statements via `;`)
5. Statement-count check via `sqlparse` — exactly one statement
6. SELECT-only check — the single statement must be a SELECT
7. **Table allowlist** — every `project.dataset.table` reference must be backtick-quoted and match one of the four known tables
8. **LIMIT clamp** — auto-inserts or caps `LIMIT {MAX_RESULT_ROWS}`
9. **Prompt-injection detection** in `sanitize_user_input()` — runs on the user's raw question BEFORE it reaches any LLM call, catching patterns like "ignore previous instructions" or fake `system:` role markers

**Edge cases handled (each was a confirmed bug, now resolved):**

- **OUT_OF_SCOPE sentinel false-rejection.** The LLM's designed escape hatch
  `SELECT 'OUT_OF_SCOPE' AS reason LIMIT 1` has zero table references by
  construction, which used to fail check 7 (table allowlist) before the
  OUT_OF_SCOPE branch could ever fire. The user saw a confusing "No
  fully-qualified table reference found" error for legitimate "can you
  visualize that?" questions. **Fix:** `cot_chat._is_out_of_scope_sql()` is
  checked BEFORE `validate()` in both the factual and chain-of-thought
  paths. Maps to a chart-aware response ("ask me to chart that") or generic
  out-of-scope response based on keyword detection.

- **Data planner false-positive DML rejection.** For broad analytical
  questions ("what are the hidden insights from this data?"), the planner
  LLM sometimes generated CTEs (`WITH ... AS (...)`) or `CREATE TEMP TABLE`,
  which tripped check 3 with "Write or admin operation detected." **Fix:**
  the COT path now distinguishes a *planner false positive* from a *genuine
  safety violation*. On a false positive, it silently substitutes a safe
  hardcoded fallback query — `SELECT platform, campaign_name, date, spend,
  conversions, cpa, ctr_pct FROM gold GROUP BY ... ORDER BY spend DESC LIMIT
  100` — logs a warning, and continues to synthesis with a "pending" status
  reasoning step. The user sees a real data-grounded answer instead of an
  error. Genuine safety violations (multi-statement injection from
  `sanitize_user_input`) still surface to the user.

### 17.5 Chain-of-Thought Chat Pipeline

#### 17.5.1 Intent classification (5 intents)

`classify_intent()` routes every question into one of:

| Intent | Routing | Example |
|---|---|---|
| `factual` | Single SQL query → narration | "What was Facebook spend?" |
| `suggestion` | Data-gathering query → numbered recommendations | "How should we improve TikTok?" |
| `analytical` | Data-gathering query → comparative synthesis | "Why is Facebook outperforming?" |
| `visualize` | Chart built from the PREVIOUS result, no new query | "Chart that as a bar chart" |
| `off_topic` | Scope-guard refusal | "Write me a poem" |

`visualize` is detected by keyword match (`chart`, `graph`, `plot`,
`visuali[sz]e`, `diagram`) BEFORE the LLM classifier runs — cheap, reliable,
and works even when the LLM is offline.

#### 17.5.2 Conversation memory + self-healing SQL

- **Last 3 turns** of conversation are threaded through the classifier, SQL
  agent/planner, and narrator — enables follow-ups ("now show TikTok", "why
  is that?").

- **Self-healing SQL** (`_execute_with_self_heal`, **`SQL_REPAIR_ATTEMPTS=4`**,
  implemented as a `while True` loop with `attempt` counter): on a recoverable
  BigQuery error (syntax, unrecognized column, "column X is neither grouped
  nor aggregated", type mismatch, invalidQuery), the original SQL + the error
  message are sent back to the LLM with instructions to return corrected SQL.
  Up to 5 total BigQuery executions per question (1 original + 4 repairs).

- **Schema discovery on repair (the key innovation).** When BigQuery returns
  "Unrecognized name: X" or "not found in", the self-heal loop calls
  `bq_client.discover_schemas_from_sql(current_sql)` which:
  1. Extracts every backtick-quoted `project.dataset.table` from the failed SQL
  2. Runs `SELECT column_name, data_type FROM project.dataset.INFORMATION_SCHEMA.COLUMNS WHERE table_name = '...'` for each (capped at 10 MB billed bytes — metadata queries are tiny)
  3. Formats the result as `"platform (STRING), metric_name (STRING), predicted_value (FLOAT64), ..."` and injects it into the repair prompt as `"ACTUAL SCHEMA — `<ref>`: ...   Use ONLY the column names shown above — do NOT invent columns."`

  Before this, repair attempts were the LLM guessing again from the same
  outdated schema description in the prompt. Now the LLM gets ground truth
  from BigQuery itself. The reasoning step in the UI shows "Schema discovered
  from INFORMATION_SCHEMA — repair will use real column names."

- **Non-recoverable errors are NOT retried** (bytes-billed cap, permission
  denied, timeout) — burning an LLM call wouldn't fix them.

#### 17.5.3 Visualize intent — constrained chart spec

When the user asks to chart the previous result:

1. The previous turn's `pd.DataFrame` is held in Streamlit session state as
   `last_data` — **kept separate from `chat_messages`** (which only stores
   JSON-serializable dicts) so we can reuse the live DataFrame on the next
   turn without round-tripping through `to_dict("records")` ↔ DataFrame.
2. The LLM (if available) receives the column names/dtypes + 3 sample rows
   (`df.head(3).to_markdown(index=False)`) and returns a JSON chart spec:
   `{chart_type, x, y, color, title}`.
3. `chart_builder.parse_chart_spec()` validates `chart_type` against a
   **5-element enum** (`bar, line, scatter, pie, area`) and `x`/`y`/`color`
   against the DataFrame's `.columns` — a hallucinated or injected column
   name (`"x": "; DROP TABLE users"`) is rejected with `ChartSpecError`.
4. On validation failure (or if the LLM is offline), `heuristic_chart_spec()`
   picks a sensible default (date column → line chart; platform column →
   bar chart; first categorical + first numeric → bar chart; empty/all-text
   df → None).
5. `build_figure()` — one of five fixed Plotly functions — renders the
   validated spec. **No LLM-generated code is ever executed.**
6. The chart spec is serialized to a plain dict before being stored in
   `chat_messages`. The `_render_assistant_message(resp_dict, msg_key)`
   helper reconstructs the `ChartSpec` dataclass and rebuilds the figure on
   every render, passing a **unique `key=f"chat_chart_{msg_key}"`** to
   `st.plotly_chart` to avoid the "multiple plotly_chart elements with the
   same auto-generated ID" crash on history replay.

This is the same "LLM picks parameters, fixed code executes" trust boundary
already used for SQL generation, generalized to chart generation. See
Section 17.7 for the full security rationale.

#### 17.5.4 Page-aware sidebar chatbot + floating AI beacon

**Sidebar chatbot.** Every page embeds a compact chat widget in the sidebar:

- The current page's context (name + summary of visible metrics) is injected
  as a synthetic prior assistant turn, so the LLM knows what the user is
  looking at.
- The user's question is delegated to the FULL `cot_chat.process_question()`
  pipeline — it can query BigQuery for things not on the current page, not
  just echo the page summary. This was a deliberate architectural choice
  after observing the v1 chatbot give "that isn't shown on this page" dead-
  end answers to legitimate questions like "why did Google do better?"
- Messages render as iMessage-style bubbles (user right/blue, bot
  left/transparent).
- **Loading state:** clicking "Ask" stores the question as "pending," reruns
  to show the user's bubble + an animated three-dot "Thinking…" bubble, THEN
  processes the LLM call, then reruns again with the answer — so the UI never
  appears frozen during a multi-second LLM round trip.

**Floating AI beacon.** A separate fixed-position UI element appears in the
main content area (bottom-right corner) on every page:

- Compact card (~252px × ~85px) with the "Ask about this page" header,
  a rotating suggestion chip (page-specific, e.g. *"Which campaign has the
  best Cost per Acquisition?"* on Performance Overview, cycling every 3.2s),
  and a hint arrow pointing toward the sidebar.
- **Perimeter "running light" animation** implemented as an **SVG `<rect>`
  with `stroke-dashoffset` animation** — a single 40px bright dash on a
  640px-empty track moves around the rectangle's border path, with a
  linear gradient fill (indigo → violet → cyan). This approach was chosen
  after three failed CSS attempts (`conic-gradient` with `transform:
  rotate()` rendered as a solid rotating disc inside Streamlit's iframe
  rendering context — `mask-composite: exclude`, the standard CSS technique
  for clipping a gradient to just a border, isn't reliable inside Streamlit).
  SVG `stroke-dashoffset` is the "train on a track" technique: declarative,
  works in every browser, no CSS hacks.
- **Aurora top bar** — 2px horizontal gradient slide animation.
- **Spring-curve entry animation** at 0.3s delay (scale 0.5 → 1, slide-up,
  `cubic-bezier(0.34,1.56,0.64,1)`).
- **Auto-collapse to a 106px "AI Chat" pill** after 9 seconds; state
  persisted in `sessionStorage` (survives Streamlit reruns without
  re-triggering the entry animation).
- **Click on body:** if collapsed, expands; if expanded, calls
  `inputs[last].focus()` + `scrollIntoView({behavior: 'smooth'})` on the
  sidebar's text input (tried via three CSS selectors progressively —
  `[data-testid="stSidebarContent"] input[type="text"]`, then broader).
  If the sidebar is collapsed, clicks the
  `[data-testid="stSidebarCollapsedControl"] button` to open it.
- **Collapse button** uses `addEventListener('click', ...)` (not inline
  `onclick=`) so it survives Streamlit's per-rerun HTML re-injection
  without function-namespace collisions.

#### 17.5.5 Narrative sanitization

Both the chat narrator and the suggestion synthesizer are instructed to
output plain prose (no markdown, no backticks). As defense-in-depth,
`_sanitize_narrative()` post-processes their output:

- Unwraps any stray inline-code spans (`` `...` `` → plain text) — fixes
  the observed mid-sentence rendering glitch where the LLM emitted a
  rogue code span breaking the prose into prose/monospace/prose fragments.
- Strips lone backticks.
- Unwraps bold/italic asterisks (`**...**` and `*...*`).
- **Does NOT touch numbered lists** (`1.` `2.` `3.`) — the suggestion
  synthesizer relies on this format for its recommendation structure.

### 17.6 LLM Resilience Layer

All LLM calls go through `llm_client.LLMClient.complete()`, which handles
five distinct failure modes against an OpenAI-compatible endpoint (Groq by
default):

| Status | Sub-type | Handling |
|---|---|---|
| **429** | **TPM** (tokens-per-minute) — body contains "tokens per minute" or "TPM" | Parses the wait from `Retry-After` header OR Groq's `"Please try again in X.Xs"` body. If a fallback model is configured AND not yet tried, switches models immediately (separate TPM bucket, no sleep). Otherwise sleeps the indicated duration. Capped at 4 attempts. |
| **429** | **TPD** (tokens-per-day) — body contains "tokens per day" or "TPD"; heuristic fallback: wait > 60s | **No retry.** Daily quotas are typically 30-60 minute waits — retrying every few seconds against a 33-minute reset would burn the entire attempt budget on nothing. Sets `self._last_tpd_wait` to a parsed user message ("The AI assistant has reached its daily usage limit. Please try again in 33m 54s."). Caller checks `llm.daily_limit_message` and surfaces it directly in `ChatResponse.content`. |
| **413** | Payload too large | **Progressive shrink-and-retry**: drops the oldest history message first (repeatedly, until only `[system, latest_user]` remains), then truncates the latest message's content to ~60% of its length per step (floor: 200 chars). Capped at **6 shrink cycles**, tracked independently from the 429/5xx retry budget. |
| **5xx** | Transient server error | Exponential backoff, capped at 4 attempts |
| timeout / connection error | Network-level failure | Exponential backoff, capped at 4 attempts |

The retry loop is a `while True` with two independent counters (`attempt`
for transient errors, `shrink_count` for 413s) plus an absolute safety cap
of `_MAX_ATTEMPTS + _MAX_SHRINK_ATTEMPTS` total iterations. All five
mechanisms are composable — a single `complete()` call can shrink a payload
(413), then hit a TPM 429 on the shrunk request, then succeed on the
fallback model, all within one call.

### 17.7 Security Decision: No LLM-Generated Code Execution

**Considered:** allowing the chatbot to generate Python (e.g. matplotlib
code) for custom visualizations.

**Rejected.** Executing LLM-generated code via `exec()`/`eval()` is arbitrary
code execution — a single prompt injection becomes a server compromise,
independent of sandboxing effort, import allowlists, or stated intent
("just for a chart").

**Built instead:** the constrained chart-spec pattern (17.5.3) — the LLM
selects FROM a closed set (5 chart types × validated column names), and five
pre-written, audited Plotly functions do the actual rendering. This mirrors
the trust boundary already established for SQL: the LLM proposes WHAT
(table/columns/chart type), a fixed validator decides whether it's safe, and
fixed code executes it. The LLM never proposes HOW (the code itself).

Verified by unit tests: a chart spec with `"x": "; DROP TABLE users"` or
`"chart_type": "exec"` is rejected by `parse_chart_spec()` with
`ChartSpecError` before any rendering happens; the system falls back to
the heuristic chart spec and continues.

The same pattern underlies the 5 AI Insights cards — each card's chart is
one of a small set of pre-built Plotly figures populated with live-queried
data, never LLM-generated rendering code.

### 17.8 AI vs. Rule-Based Fallback Labeling

Every AI-generated insight is labeled with its actual source:

- `AI · llama-3.3-70b-versatile` (or whichever model responded) — badge shown
  on each of the 5 AI Insights cards when the LLM call succeeded.
- **Rule-based fallback** — if the LLM is unavailable, times out, returns
  an empty response, or hits a 413/429 that can't be recovered,
  `insight_generator.py` produces a deterministic summary from the same
  underlying data using string templating, labeled accordingly. The
  dashboard never shows an empty card or a raw error in place of an insight.

The Home page footer shows the aggregate: e.g. "5/5 AI-generated · 0/5
rule-based fallback · Cache TTL 10 minutes" — giving stakeholders visibility
into whether they're looking at live model output or the deterministic
fallback.

### 17.9 Deprecation API Migrations

The Streamlit codebase was migrated from deprecated API surfaces during
development:

- `pd.Timestamp.utcnow()` → `pd.Timestamp.now(tz='UTC')` (pandas 4 removed
  the helper).
- All 23 occurrences of `use_container_width=True` → `width='stretch'`
  across `Home.py`, `lib/page_chatbot.py`, and `pages/*.py` (Streamlit
  removed the kwarg after 2025-12-31).

`grep -rn "use_container_width\|Timestamp.utcnow" --include="*.py"` returns
zero results in `streamlit_app/`.

### 17.10 Deployment

Per `STREAMLIT_ARCHITECTURE.md` Section 12.4 — Streamlit Community Cloud:

1. Push `streamlit_app/` to GitHub.
2. Connect the repo at https://share.streamlit.io, entry point `Home.py`.
3. Set secrets in the Streamlit Cloud dashboard: `LLM_ENDPOINT_URL`,
   `LLM_BEARER_TOKEN`, `LLM_TARGET_MODEL`, `LLM_FALLBACK_MODEL` (optional),
   `GCP_PROJECT_ID`, `GCP_SERVICE_ACCOUNT_JSON` (full JSON in one secret).
4. The resulting public URL is the "Live dashboard link" for submission
   (Section 19).

**Status:** not yet deployed — see `PROJECT_STATUS_REPORT.md` Section 7.1 for
the step-by-step checklist.

### 17.11 Gaps vs. the Original Looker Spec

The original Looker Studio spec (now superseded) called for three operations-
oriented features that do **not** currently exist in the Streamlit build.
Listed here for completeness — these are NOT required for submission but
would strengthen it:

| Original feature | Status in Streamlit | Effort to add |
|---|---|---|
| **Data Freshness Header** — "Last refreshed", "Latest files processed", "Quarantined rows in last run" from `tbl_ingestion_audit` / `stg_quarantine_logs` | ❌ Not built. Streamlit's cache footer shows "5-min cache" but not pipeline-run freshness. | ~30 min — small `st.metric` row on Home reading the audit table, IF `tbl_ingestion_audit` is populated by pipeline script 01 (unverified — see `PROJECT_STATUS_REPORT.md` Section 2.1) |
| **Lookback Window Indicators** — per-widget footnotes ("Analysis window: 90 days") | ⚠️ Partial. The AI Insights anomaly explainer documents windows in prose; not surfaced as small footnotes on every Tier-2 widget. | ~20 min — add a one-line caption under each AI Insights card |
| **Operations Dashboard (3rd page)** — ingestion audit log, quarantine breakdown, archival counts | ❌ Not built. Streamlit has 5 pages (Home + 4 numbered), none operational. | ~2 hrs — new `pages/5_⚙️_Operations.py` reading `tbl_ingestion_audit` + `stg_quarantine_logs`, IF those tables are populated |

All three depend on Tier 1 pipeline tables (`tbl_ingestion_audit`,
`stg_quarantine_logs`) that the dashboard sprint did not verify. If
pipeline scripts 01/02 are not yet writing to those tables per the v2.0
spec (Sections 7, 8), building these Streamlit features would have nothing
to read — verify the Tier 1 scripts first (`PROJECT_STATUS_REPORT.md`
Sections 2.1-2.2).

**Conversely**, the Streamlit build added several features the original
Looker spec did NOT call for: the chain-of-thought chat with self-healing
SQL + schema discovery, the visualize intent with constrained chart spec,
the page-aware sidebar chatbot, the floating AI beacon, the Platform
Strength Radar, the Conversion Quality funnel chart, the AI/rule-based
fallback labeling system, and the full LLM resilience layer (429 TPM/TPD
+ 413 shrink-and-retry).

---

## 18. Step 10: README + Step 11: Video Script

README structure (unchanged from v1.0 conceptually):

- Business problem
- Architecture diagram (Tier 1 + Tier 2)
- Setup
- Execution order
- Key findings
- Live dashboard link
- Video link

Video script (8 minutes target):

| Time | Section | Talking points |
|---|---|---|
| 0:00–0:45 | Framing | Real business problem, not homework |
| 0:45–1:45 | Architecture | Two-tier event-driven design, Eventarc trigger, MERGE incremental pattern |
| 1:45–2:30 | Tier 1 walkthrough | Schema differences, validation layers (Bronze + Gold), audit logging |
| 2:30–4:30 | Dashboard Page 1 | KPIs, budget allocation finding, campaign-level CPA gap, TikTok funnel |
| 4:30–6:00 | Dashboard Page 2 | ML layer — anomaly, optimizer (+1,526 conversions), forecast, AI summary |
| 6:00–7:00 | Operational excellence | QA validation, file archival, cost guardrails, schema evolution |
| 7:00–7:45 | Production constraints | Time zones, currencies, dead letter queue, Secret Manager — what we'd add at scale |
| 7:45–8:00 | Close | GitHub link, contact |

---

## 19. Submission Checklist

```
Tier 1 (event-driven layer):
[ ] 01_validate_and_ingest.py refactored to MERGE pattern
[ ] 02_run_transformations.py auto-detects first-run vs incremental
[ ] Bronze tables have ingested_at + source_file columns
[ ] Quarantine table uses deterministic MD5 IDs
[ ] Audit table tracks status transitions
[ ] File archival logic implemented (or documented for assignment scope)

Tier 2 (derived analytics):
[ ] 03_anomaly_detection.py uses 90-day lookback window
[ ] 04_budget_optimizer.py uses 30-day lookback window (already done)
[ ] 05_forecast.py uses Holt-Winters with SES fallback, 60-day window
[ ] 06_llm_executive_summary.py is model-agnostic, has rule-based fallback
[ ] All Tier 2 scripts have explicit OUTPUT_SCHEMA + retry logic

Validation:
[ ] Bronze validation rules R0-R7 in 01_validate_and_ingest.py
[ ] Gold validation rules G1-G12 in gold_unified_performance_mart.sql
[ ] Rejected rows route to quarantine with structured reasons

Operational:
[ ] Cost guardrails (maximum_bytes_billed) on every BQ operation
[ ] Audit + quarantine tables have partition_expiration_days=90
[ ] Schema evolution policy documented
[ ] Cloud Function code in cloud_function/ directory

Verification:
[ ] 07_qa_validation.py uses dynamic row count, hardcoded column count
[ ] All financial totals reconcile bronze == gold within $0.01
[ ] Dashboard published with public shareable link
[ ] Video recorded with both production thinking + actual numbers
[ ] GitHub repo committed with README pointing to dashboard + video

Submission:
[ ] Google Form submitted with both links
[ ] Submitted before deadline
```

---

## 20. Production Constraints (Phase 2 Evolution)

> Documented as known constraints to surface in the video walkthrough. Calling
> these out demonstrates senior-level edge-case awareness.

### 20.1 Time Zone Handling

**Current state:** All dates assumed UTC. Single advertiser, single time zone.

**Production gap:** Marketing platforms report data in the advertiser's account
time zone. A "January 30" row from PST overlaps with January 29/30 UTC.

**Phase 2 fix:** Add `advertiser_timezone` column to bronze schema. Normalize
to UTC in gold transformation. Add a `local_date` column for tz-aware reporting.

### 20.2 Currency Handling

**Current state:** All spend assumed USD.

**Production gap:** Facebook/Google/TikTok all report in advertiser account
currency. Blending EUR + USD + JPY produces nonsense totals.

**Phase 2 fix:** Add `currency_code` column. Maintain a daily FX rate table.
Add `spend_usd` computed column in gold.

### 20.3 Secrets Management

**Current state:** API keys via env vars.

**Production gap:** Cloud Function env vars are plaintext in function metadata.

**Phase 2 fix:** Google Secret Manager. Already supported in deployment YAML
(`--set-secrets=LLM_BEARER_TOKEN=...`).

### 20.4 PII Detection

**Current state:** Dataset has no PII.

**Production gap:** Campaign names occasionally include customer email patterns.

**Phase 2 fix:** PII scan at bronze ingestion. Quarantine + alert on detection.

### 20.5 RBAC

**Current state:** Single service account with project-wide access.

**Production gap:** Should isolate read-only dashboard SA from write-capable
pipeline SA.

**Phase 2 fix:** Two service accounts, dataset-level IAM grants.

### 20.6 Workflow Orchestration

**Current state:** Bash script chains Tier 2 scripts in order.

**Production gap:** No retry orchestration, no dependency graphs, no SLAs.

**Phase 2 fix:** Cloud Composer (managed Airflow) or Cloud Workflows for
orchestration. DAGs version-controlled. SLO/SLA monitoring.

### 20.7 Unit + Integration Tests

**Current state:** No tests.

**Phase 2 fix:**
- Unit tests for `DataQualityGate` (every rule with known good/bad inputs)
- Unit tests for `AnomalyDetectionEngine._compute_anomalies` (Z-score math against fixtures)
- Integration test running full pipeline against tiny fixture dataset

### 20.8 Data Lineage

**Current state:** Lineage implicit in code.

**Phase 2 fix:** Migrate to dbt. dbt-elementary for column-level lineage. Data
Catalog for cross-team discovery.

---

## Appendix A — Campaign-Level Performance Reference

| Platform | Campaign | Spend | Impressions | Clicks | Conversions | CPA | CTR | CPM |
|---|---|---|---|---|---|---|---|---|
| Google | Search_Brand_Terms | $7,368 | ~739K | ~1,450 | 1,445 | **$5.10** | 5.22% | $9.96 |
| Facebook | Conversions_Retargeting | $6,371 | ~370K | ~1,380 | 1,070 | $5.95 | 4.63% | $17.25 |
| Google | Shopping_All_Products | $11,417 | ~1.9M | ~1,870 | 1,801 | $6.34 | 3.34% | $6.10 |
| Facebook | Traffic_Drive_Jan | $5,574 | ~993K | ~820 | 741 | $7.52 | 3.68% | $5.61 |
| Google | Display_Remarketing | $3,352 | ~3.4M | ~350 | 345 | $9.72 | 0.36% | $0.99 |
| Facebook | Brand_Awareness_Q1 | $4,088 | ~1.4M | ~893 | 433 | $9.44 | 2.03% | $2.87 |
| TikTok | Influencer_Collab | $26,312 | ~10.5M | ~4,270 | 2,653 | $9.92 | 1.62% | $2.50 |
| TikTok | Conversion_Focus | $20,606 | ~3.97M | ~2,350 | 2,061 | $10.00 | 1.91% | $5.19 |
| TikTok | Awareness_GenZ | $15,640 | ~8.1M | ~3,460 | 1,203 | $13.00 | 1.47% | $1.94 |
| Facebook | Video_Views_Campaign | $2,259 | ~1.75M | ~302 | 151 | $14.96 | 0.36% | $1.29 |
| TikTok | Traffic_Campaign | $11,708 | ~6.2M | ~2,330 | 833 | $14.06 | 1.57% | $1.90 |
| Google | Search_Generic_Terms | $15,549 | ~1.22M | ~2,447 | 627 | **$24.80** | 1.99% | $12.79 |

---

## Appendix B — Complete Column Mapping Matrix

| Unified Column | Facebook | Google | TikTok | Type | Bronze Rule | Gold Rule |
|---|---|---|---|---|---|---|
| `date` | `date` | `date` | `date` | DATE | R1 | (used in partition) |
| `platform` | literal | literal | literal | STRING | (set in CTE) | (set in CTE) |
| `campaign_id` | `campaign_id` | `campaign_id` | `campaign_id` | STRING | R0 | NOT NULL |
| `campaign_name` | `campaign_name` | `campaign_name` | `campaign_name` | STRING | R6 | — |
| `sub_group_id` | `ad_set_id` | `ad_group_id` | `adgroup_id` | STRING | R0, R6 | NOT NULL |
| `sub_group_name` | `ad_set_name` | `ad_group_name` | `adgroup_name` | STRING | R6 | — |
| `impressions` | `impressions` | `impressions` | `impressions` | INT64 | R3a, R3b | G2 |
| `clicks` | `clicks` | `clicks` | `clicks` | INT64 | R3a, R3b | G3 |
| `spend` | `spend` | `cost` | `cost` | FLOAT64 | R2a, R2b | G1 |
| `conversions` | `conversions` | `conversions` | `conversions` | INT64 | R3a, R3b | G4 |
| `conversion_value` | 0.0 default | `conversion_value` | 0.0 default | FLOAT64 | R5 | G5 |
| `video_views` | `video_views` | 0 default | `video_views` | INT64 | R4 | — |
| `video_watch_25/50/75/100` | 0 defaults | 0 defaults | `video_watch_*` | INT64 | R4 | G11, G12 |
| `likes` | 0 default | 0 default | `likes` | INT64 | R4 | — |
| `shares` | 0 default | 0 default | `shares` | INT64 | R4 | — |
| `comments` | 0 default | 0 default | `comments` | INT64 | R4 | — |
| `reach` | `reach` | 0 default | 0 default | INT64 | R4 | G10 |
| `frequency` | `frequency` | 0.0 default | 0.0 default | FLOAT64 | R5 | G7 |
| `engagement_rate` | `engagement_rate` | 0.0 default | 0.0 default | FLOAT64 | R5 | G8 |
| `quality_score` | 0 default | `quality_score` | 0 default | INT64 | R4 | G6 |
| `search_impression_share` | 0.0 default | `search_impression_share` | 0.0 default | FLOAT64 | R5 | G9 |
| `avg_cpc` | 0.0 default | `avg_cpc` | 0.0 default | FLOAT64 | R5 | — |
| `cpa` | Computed | Computed | Computed | FLOAT64 | — | — |
| `ctr` | Computed | Computed | Computed | FLOAT64 | — | — |
| `cpc` | Computed | Computed | Computed | FLOAT64 | — | — |
| `cpm` | Computed | Computed | Computed | FLOAT64 | — | — |
| `roas` | 0.0 (no revenue) | Computed | 0.0 (no revenue) | FLOAT64 | — | — |
| `ingested_at` | (set in script) | (set in script) | (set in script) | TIMESTAMP | — | — |
| `source_file` | (set in script) | (set in script) | (set in script) | STRING | — | — |

**Total Gold columns: 30** (Data Contract — hardcoded check in QA)

---

*Document version: 2.0*
*Prepared for: Improvado Senior Marketing Analyst Assessment*
*Architecture: Event-driven two-tier lakehouse with MERGE incremental loads*
*All data findings verified against actual source CSV files. Financial checkpoints hardcoded from profiling.*