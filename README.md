# Cross-Channel Marketing Analytics Lakehouse

**Improvado Senior Marketing Analyst Technical Assessment**

> An event-driven lakehouse that unifies Facebook, Google, and TikTok ad
> performance data — with ML enrichment, AI-generated insights, and a
> conversational analytics interface — built entirely on BigQuery + Python.

---

## 🚀 Live App

**[https://improvado-mohiddin.streamlit.app/](https://improvado-mohiddin.streamlit.app/)**

Five-page Streamlit dashboard: Performance Overview · AI Insights ·
Channel Deep Dives · Chat With Your Data · (Home).

---

## Business Problem

A marketing team spending **$130,244.90/month** across three platforms has no
unified performance view. Each platform uses different metric names, different
conversion definitions, and different reporting structures. This pipeline
answers four questions the CMO currently cannot answer without manual
spreadsheet work:

1. Which channel has the lowest cost per acquisition?
2. Is performance improving or degrading week over week?
3. Where should the next incremental dollar go?
4. What is our blended ROAS across all channels?

---

## Key Findings

| Metric | Value |
|---|---|
| Total spend (Jan 2024) | **$130,244.90** |
| Total conversions | **13,363** |
| Blended CPA | **$9.75** |
| Best-performing channel | Facebook — $7.64 CPA (14% of budget) |
| Worst-performing channel | TikTok — $11.00 CPA (57% of budget) |
| Worst single campaign CPA | Google `Search_Generic_Terms` — **$24.80** |
| Best single campaign CPA | Google `Search_Brand_Terms` — **$5.10** |
| Budget reallocation opportunity | **+1,526 conversions (+11.4%)** at same spend |

**Primary insight:** TikTok receives 57% of budget but delivers the worst CPA.
Facebook receives 14% but delivers the best CPA. Reallocating toward Facebook
and Google (capped at 2× current) yields +1,526 additional conversions with
zero incremental spend.

---

## Architecture

### Two-Tier Design

```
┌─────────────────────────────────────────────────────────────────────┐
│  GCS Bucket: improvado-analytics-lakehouse-raw                       │
│  ├─ incoming/        ← Upload CSVs here (Eventarc watches this)      │
│  ├─ archive/YYYY/MM/ ← Successfully processed files                  │
│  └─ failed/          ← Failed ingestion (with error metadata)        │
└────────────────────────────┬────────────────────────────────────────┘
                             │  google.cloud.storage.object.v1.finalized
                             │  (Eventarc, ~2–10s latency)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  TIER 1 — EVENT-DRIVEN INGESTION + TRANSFORMATION                    │
│                                                                      │
│  01_validate_and_ingest.py                                           │
│   ├─ Platform detection from filename                                │
│   ├─ Schema validation (Python: types, structural, ranges)           │
│   ├─ MERGE → bronze landing on (date, campaign_id, sub_group_id)    │
│   ├─ Audit logging at every status transition                        │
│   └─ File archival: incoming/ → archive/ or failed/                 │
│                                                                      │
│  02_run_transformations.py                                           │
│   ├─ Auto-detects first-run (CREATE) vs incremental (MERGE)         │
│   ├─ Per-platform gold MERGE wrapped in BEGIN/COMMIT transaction     │
│   ├─ Gold business validation (rules G1–G12 in SQL CTE layer)       │
│   └─ Rejected rows routed to stg_quarantine_logs                    │
│                                                                      │
│  Result: Bronze + Gold always current, full history preserved        │
└────────────────────────────┬────────────────────────────────────────┘
                             │  Scheduled (Cloud Scheduler) or on-demand
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  TIER 2 — DERIVED ANALYTICS (WRITE_TRUNCATE on lookback windows)     │
│                                                                      │
│  03_anomaly_detection.py   — 90-day window, MAD-based Z-score       │
│  04_budget_optimizer.py    — 30-day window, scipy linprog (HiGHS)   │
│  05_forecast.py            — 60-day window, Holt-Winters + SES      │
│  (06 retired → lives in Streamlit insight_generator.py)             │
│  07_qa_validation.py       — reconciliation runner, 9/9 checks PASS │
│                                                                      │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STREAMLIT DASHBOARD  (reads Gold + Tier 2 tables via BQ client)    │
│                                                                      │
│  Home.py                          Executive summary + headline KPIs  │
│  1_📊_Performance_Overview.py     KPI strip · trend · platform tbl  │
│  2_💡_AI_Insights.py             5 AI cards w/ LLM + rule fallback  │
│  3_🎯_Channel_Deep_Dives.py      Per-platform tabs (FB/GG/TT)       │
│  4_💬_Chat_With_Your_Data.py     Chain-of-thought SQL chatbot        │
│                                                                      │
│  https://improvado-mohiddin.streamlit.app/                          │
└─────────────────────────────────────────────────────────────────────┘
```

### Architecture Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Cloud database | BigQuery | Free tier, native Python client, partition+cluster optimization, cost guardrails |
| Trigger | Eventarc + Cloud Function 2nd gen | True event-driven; GCS-native `object.finalized` event |
| Bronze pattern | All-STRING landing | Insulates pipeline from upstream type changes |
| Schema evolution | Permissive Bronze, Strict Gold | Bronze absorbs new columns; Gold stays at exactly 30 columns |
| Anomaly method | MAD-based Z-score (rolling 7-day, per group) | Robust to outliers; interpretable; bidirectional |
| Forecast | Holt-Winters with SES fallback | Captures weekly seasonality; SES fallback for sparse campaigns |
| Optimizer | scipy linprog (HiGHS) | Lightweight LP; produces actionable CMO-level output |
| GenAI | OpenAI-compatible REST (model-agnostic) | Works with Groq / OpenRouter / Gemini / OpenAI / local Ollama |

---

## Dashboard Pages

| Page | What it shows |
|---|---|
| **Home** | Computed headline KPIs, architecture summary, navigation |
| **📊 Performance Overview** | 4-metric KPI strip, daily spend trend, platform comparison table, campaign scatter (spend vs CPA), channel-specific charts (FB reach×frequency, GG quality score, TT video funnel) |
| **💡 AI Insights** | 5 auto-generated cards: Executive Briefing, Worst Performer, Budget Reallocation, 14-Day Forecast Outlook, Anomaly Patterns — each with LLM narrative and rule-based fallback |
| **🎯 Channel Deep Dives** | Tabbed per-platform view: Facebook reach/frequency, Google ROAS and quality score, TikTok video completion funnel |
| **💬 Chat With Your Data** | Conversational SQL chatbot with chain-of-thought reasoning, self-healing SQL retry, conversation memory (last 3 turns), scope guard, 9-layer SQL validator |

**Chat example questions:**

- "Which platform has the worst CPA?" → TikTok at $11.00
- "What was total spend on Facebook?" → $18,292.00
- "Show me the top 5 campaigns by spend"
- "Should I increase TikTok's budget?"

---

## Repository Structure

```
improvado-sr-mrkt-assmt/
├── .env.local                                  # gitignored — copy from .env.example
├── .gitignore
├── requirements.txt                            # root, consolidated
├── sync_requirements.py
├── marketing-analytics-pipeline/
│   ├── config/
│   │   ├── platform_schema_registry.json       # per-platform column metadata + rules
│   │   └── validation_bounds_criteria.json     # range thresholds per column
│   ├── data/
│   │   └── raw/
│   │       ├── 01_facebook_ads.csv             # 110 rows, Jan 2024
│   │       ├── 02_google_ads.csv               # 110 rows, Jan 2024
│   │       └── 03_tiktok_ads.csv               # 110 rows, Jan 2024
│   ├── documentation/
│   │   ├── MASTER_LLD_v2.0_IMPROVADO_ASSESSMENT.md
│   │   ├── STREAMLIT_ARCHITECTURE.md
│   │   └── Dashboard design brief.md
│   ├── logs/                                   # *.log/*.txt gitignored
│   ├── pipelines/
│   │   ├── 01_validate_and_ingest.py           # Tier 1: Bronze MERGE
│   │   ├── 02_run_transformations.py           # Tier 1: Gold MERGE
│   │   ├── 03_anomaly_detection.py             # Tier 2: MAD Z-score, 90-day
│   │   ├── 04_budget_optimizer.py              # Tier 2: linprog, 30-day
│   │   ├── 05_forecast.py                      # Tier 2: Holt-Winters, 60-day
│   │   └── 07_qa_validation.py                 # QA reconciliation (9/9 checks PASS)
│   ├── queries/
│   │   ├── gold_unified_performance_mart.sql   # Gold transformation
│   │   └── qa_reconciliation_checks.sql        # QA queries
│   ├── requirements.txt                        # pipeline-specific
│   └── tests/
│       └── test_anomaly_detection.py           # 7/7 unit tests passing
└── streamlit_app/                              # multi-page Streamlit app
    ├── Home.py                                 # executive dashboard home
    ├── requirements.txt                        # Streamlit-specific deps
    ├── .streamlit/
    │   ├── config.toml                         # theme
    │   └── secrets.toml                        # gitignored — see setup below
    ├── lib/
    │   ├── bq_client.py                        # cached BQ client + run_query
    │   ├── chart_builder.py                    # Plotly chart helpers
    │   ├── config.py                           # env + secrets reader
    │   ├── cot_chat.py                         # chain-of-thought pipeline
    │   ├── formatters.py                       # $130.2K / 13.4K compact format
    │   ├── glossary.py                         # full-form metric labels
    │   ├── insight_generator.py                # 5 AI cards, LLM + fallback
    │   ├── llm_client.py                       # 429 retry, model fallback
    │   ├── page_chatbot.py                     # sidebar chatbot UI
    │   ├── page_style.py                       # shared CSS
    │   ├── prompts.py                          # all system prompts
    │   ├── schema_registry.py                  # 30-column gold schema
    │   ├── scope_guard.py                      # off-topic rejection
    │   ├── sql_agent.py                        # schema-grounded SQL generation
    │   └── sql_validator.py                    # 9-layer SQL safety validator
    └── pages/
        ├── 1_📊_Performance_Overview.py
        ├── 2_💡_AI_Insights.py
        ├── 3_🎯_Channel_Deep_Dives.py
        └── 4_💬_Chat_With_Your_Data.py
```

---

## BigQuery Schema

**Project:** `improvado-analytics-lakehouse`

| Dataset | Table | Purpose |
|---|---|---|
| `improvado_analytics_bronze` | `facebook_ads_landing` | Raw Facebook data (all-STRING) |
| `improvado_analytics_bronze` | `google_ads_landing` | Raw Google data (all-STRING) |
| `improvado_analytics_bronze` | `tiktok_ads_landing` | Raw TikTok data (all-STRING) |
| `improvado_analytics_production` | `fct_unified_marketing_performance` | Gold mart (30 cols, partitioned by date, clustered by platform/campaign_id) |
| `improvado_analytics_staging` | `fct_anomaly_flags` | Tier 2: anomaly detection output |
| `improvado_analytics_staging` | `tbl_budget_recommendations` | Tier 2: optimizer output |
| `improvado_analytics_staging` | `tbl_forecast` | Tier 2: forecast output |
| `improvado_analytics_staging` | `stg_quarantine_logs` | Rows failing Bronze/Gold validation |
| `improvado_analytics_staging` | `tbl_ingestion_audit` | Per-run status transitions |

---

## Setup

### Prerequisites

- Python 3.11+
- Google Cloud project with BigQuery API enabled
- Service account with `BigQuery Data Editor` + `BigQuery Job User` roles
- Application Default Credentials: `gcloud auth application-default login`

### 1. Clone and install

```bash
git clone https://github.com/<your-handle>/improvado-sr-mrkt-assmt.git
cd improvado-sr-mrkt-assmt
pip install -r requirements.txt
```

### 2. Environment variables

Copy `.env.example` to `.env.local` and fill in:

```bash
# Google Cloud
GCP_PROJECT_ID=improvado-analytics-lakehouse
GCS_BUCKET=improvado-analytics-lakehouse-raw

# LLM (OpenAI-compatible endpoint — works with Groq, OpenRouter, Ollama, etc.)
LLM_ENDPOINT_URL=https://api.groq.com/openai/v1/chat/completions
LLM_BEARER_TOKEN=gsk_...
LLM_TARGET_MODEL=llama-3.3-70b-versatile
```

### 3. Streamlit secrets (local dev)

Create `streamlit_app/.streamlit/secrets.toml` (gitignored):

```toml
[llm]
endpoint_url = "https://api.groq.com/openai/v1/chat/completions"
bearer_token = "gsk_..."
target_model  = "llama-3.3-70b-versatile"

[gcp]
project_id = "improvado-analytics-lakehouse"
# For Streamlit Cloud deployment, also add:
# service_account_json = """{ "type": "service_account", ... }"""
```

---

## Execution Order

Run scripts in order. Each step is a prerequisite for the next.

```bash
# Tier 1 — must run first
python marketing-analytics-pipeline/pipelines/01_validate_and_ingest.py
python marketing-analytics-pipeline/pipelines/02_run_transformations.py

# Tier 2 — derived analytics (can re-run independently)
python marketing-analytics-pipeline/pipelines/03_anomaly_detection.py
python marketing-analytics-pipeline/pipelines/04_budget_optimizer.py
python marketing-analytics-pipeline/pipelines/05_forecast.py

# QA validation — confirms Bronze == Gold to the penny
python marketing-analytics-pipeline/pipelines/07_qa_validation.py

# Dashboard — reads current BigQuery state
cd streamlit_app
streamlit run Home.py
```

**QA validation output (verified 2026-06-14):**

```
[1/9] Row count reconciliation      PASS — gold + quarantine == bronze (330 == 330)
[2/9] Column count (Data Contract)  PASS — gold has 30 columns (expected 30)
[3/9] Per-platform spend            PASS — Facebook $18,292.00 | Google $37,686.20 | TikTok $74,266.70
[4/9] Per-platform conversions      PASS — FB 2,395 | GG 4,218 | TT 6,750
[5/9] Grand total spend             PASS — $130,244.90 Δ=$0.00
[6/9] Grand total conversions       PASS — 13,363
[7/9] No negative spend in gold     PASS
[8/9] No null platform in gold      PASS
[9/9] ML tables populated           PASS
Exit code: 0
```

---

## Operational Features

| Feature | Implementation |
|---|---|
| Idempotent ingestion | MERGE on composite key `(date, campaign_id, sub_group_id)` |
| File archival | `incoming/` → `archive/YYYY/MM/` on success, `failed/` on error |
| Audit logging | Status transitions STARTED → VALIDATING → MERGING → SUCCESS/FAILED |
| Cost guardrails | `maximum_bytes_billed` on every BigQuery operation |
| Validation | Bronze rules R0–R7 (Python) + Gold rules G1–G12 (SQL CTE) |
| Quarantine | Bad rows → `stg_quarantine_logs` with deterministic MD5 IDs |
| Schema evolution | Permissive Bronze (`ALLOW_FIELD_ADDITION`), strict Gold (30-column contract) |
| LLM resilience | 4 retries with `retry-after` parsing, TPM-fallback model switch |
| SQL safety | 9-layer validator: length · comment-strip · double-keyword · suspicious patterns · statement count · SELECT-only · table allowlist · LIMIT clamp · prompt-injection |

---

## Test Coverage

```bash
# Anomaly detection — 7/7 passing
python -m pytest marketing-analytics-pipeline/tests/test_anomaly_detection.py -v

# Streamlit service layer — 28+ unit tests
python -m pytest streamlit_app/tests/ -v
```

---

## Production Constraints (Phase 2)

Known gaps documented for the video walkthrough. Each has a defined fix path:

- **Time zones:** All dates assumed UTC. Phase 2: add `advertiser_timezone` → normalize to UTC in gold, expose `local_date` column.
- **Currency:** All spend assumed USD. Phase 2: add `currency_code` + daily FX rate table + `spend_usd` computed column.
- **Secrets:** API keys via env vars. Phase 2: Google Secret Manager (`--set-secrets` in Cloud Run deployment YAML).
- **Event trigger:** Cloud Function code documented in `documentation/` but not deployed (assignment uses CLI invocation). Phase 2: Eventarc trigger on GCS `object.finalized`.

---

## Video Walkthrough

> 🎬 *Link to be added after recording*

5–8 minute Loom walkthrough covering:
business problem → architecture → Tier 1 pipeline → dashboard pages →
ML insights (+1,526 conversions from reallocation) → QA validation →
production constraints.