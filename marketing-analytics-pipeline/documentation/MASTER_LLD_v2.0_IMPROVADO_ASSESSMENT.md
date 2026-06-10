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
17. [Step 9: Dashboard Specification (Looker Studio)](#17-step-9-dashboard-specification-looker-studio)
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
| Live dashboard link | Yes | Looker Studio, two pages |
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
                          Looker Studio Dashboard
                  (always reads current Tier 2 + Gold state)
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
| Cloud database | BigQuery | Free tier, native Looker Studio connector, partition+cluster optimization, ADC auth |
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
| Gold table does not exist | `CREATE OR REPLACE TABLE ... PARTITION BY date CLUSTER BY platform, campaign_id` |
| Gold table exists, incremental run | `MERGE` source bronze data into gold on composite key |
| Bronze data updated via restatement | Existing gold row UPDATE with corrected values |
| New platform added | Inserts new platform rows; existing platforms unchanged |

### 8.2 Composite Key for Gold MERGE

`(date, platform, campaign_id, sub_group_id)` — note `platform` is needed here
because the gold table unifies all three platforms into one table.

### 8.3 Two Execution Paths

The script auto-detects which path to take:

```python
def _gold_table_exists(self) -> bool:
    try:
        self._client.get_table(self.GOLD_TABLE)
        return True
    except NotFound:
        return False

def run(self):
    if self._gold_table_exists():
        self._execute_incremental_merge()
    else:
        self._execute_first_time_create()
```

### 8.4 First-Run Path (CREATE OR REPLACE)

Reuses the `gold_unified_performance_mart.sql` from v1.0 — produces 30 columns
with partition+cluster optimization.

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
| **G8** | `engagement_rate BETWEEN 0.0 AND 1.0` | engagement_rate | Range | Reject + quarantine |
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

---

## 14. Tier 2 — Step 6: GenAI Executive Summary (Model-Agnostic)

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

### 14.4 Output Schema

`improvado_analytics_staging.tbl_ai_executive_summary` (WRITE_TRUNCATE, 1 row):

| Column | Type |
|---|---|
| `generated_at` | TIMESTAMP |
| `summary_text` | STRING |
| `context_json` | STRING |
| `model_used` | STRING |

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

## 17. Step 9: Dashboard Specification (Looker Studio)

Identical to v1.0 (see original Section 15) plus these v2.0 additions:

### 17.1 Page 1 — New: Data Freshness Header

A small scorecard at the top-right reading from `tbl_ingestion_audit`:

- **Last refreshed:** `MAX(completed_at) WHERE status = 'SUCCESS'`
- **Latest files processed:** Comma-list of `file_name` from last 24 hours
- **Quarantined rows in last run:** `SUM(rows_quarantined) WHERE started_at > NOW() - 24h`

This widget answers "is the dashboard data current?" for stakeholders.

### 17.2 Page 2 — Lookback Window Indicators

Each Tier 2 widget includes a small footnote:
- Anomaly section: "Analysis window: 90 days"
- Optimizer section: "Analysis window: 30 days"
- Forecast section: "Trained on 60 days; projecting 14 days forward"

Sets expectations for the stakeholder.

### 17.3 Page 3 — NEW: Operations Dashboard (optional)

For the deep-dive interview, demonstrate operational awareness with a 3rd page:

- Ingestion audit log (last 30 days, status breakdown)
- Quarantine count by platform and rule type
- Average ingestion duration per platform
- File archival counts (incoming / archive / failed bucket sizes)

Built from `tbl_ingestion_audit` and `stg_quarantine_logs`.

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