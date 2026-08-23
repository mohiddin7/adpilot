# AdPilot

![ci](https://github.com/mohiddin7/adpilot/actions/workflows/ci.yml/badge.svg) ![evals](https://img.shields.io/badge/evals-80%25-yellow)

**An agentic analytics platform for marketing data.** Unifies multi-channel ad performance in a BigQuery lakehouse, enriches it with anomaly detection, forecasting and budget optimization, and puts an AI analyst on top that answers questions in plain English — with guardrails, self-healing SQL and an evaluation harness.

> Status: active rebuild. The agent core now runs on Pydantic AI with a data-agnostic "pack" system. The FastAPI service and the daily brief are live; next up is an MCP server. See the roadmap below.

## What it does today

- **Agent** (`adpilot/`): one Pydantic AI agent with typed tools (`run_sql`, `get_anomalies`, `get_forecast`, `get_budget_plan`, `render_chart`) over any SQL source. Model-written SQL goes through a defense-in-depth validator; data-source errors are returned to the model as structured hints so it repairs its own query (max 3 executions, 4 model calls per question). Primary → fallback model chain on 429s, then a deterministic rule-based answer so the user never sees a stack trace.
- **Packs** (`packs/ads/`): everything domain-specific — table allowlist, column descriptions, glossary, system prompt, canned fallback queries and a DuckDB bootstrap. Swap the directory to point the same agent at different data.
- **Connectors**: BigQuery (bytes-billed cap) and DuckDB over the raw CSVs, so the agent and tests run with zero credentials.
- **Pipeline** (`pipelines/`): validate → Bronze MERGE → Gold MERGE (30-column contract) → anomaly flags (MAD z-score) → budget optimizer (LP) → 14-day forecast (Holt-Winters) → QA reconciliation. Idempotent, audited, cost-capped.
- **Dashboard** (`streamlit_app/`): performance overview, per-channel deep dives, AI insight cards and chat. Being replaced by a pack-driven dashboard in Phase 4.

## Roadmap

| Phase | Deliverable |
|---|---|
| 0 | Repo hygiene, env-driven config, CI ✅ |
| 1 | Data-agnostic agent core (Pydantic AI), typed tools, guardrails, `adpilot chat` CLI ✅ |
| 2 | Eval harness: golden cases, red-team, self-heal rate, LLM judge, scorecard ✅ |
| 3 | Three surfaces over the same agent: FastAPI service ✅ · daily brief ✅ · MCP server |
| 4 | Pack-driven dashboard + human-in-the-loop budget approvals |
| 5 | Production rollout: Docker ✅, tracing ✅, deployed Cloud Run service |

## Quick start

```bash
git clone https://github.com/mohiddin7/adpilot.git && cd adpilot
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env        # add AGENT_LLM_BEARER_TOKEN; BigQuery values only if you use that connector
```

Ask the agent (DuckDB over the bundled CSVs — no cloud account needed):

```bash
adpilot --connector duckdb chat -q "Which campaign has the worst cost per acquisition?"
adpilot --connector duckdb chat --session demo      # REPL; remembers the last 3 turns
adpilot --connector duckdb schema                   # what the agent can query
```

Without an API key the CLI still answers common questions from the pack's pre-defined queries and says so.

## API

```bash
export ADPILOT_API_KEY=$(python -c "import secrets;print(secrets.token_hex(16))")
uvicorn --factory adpilot.api.app:create_app --workers 1
```

```bash
curl -sX POST localhost:8000/ask \
  -H "X-API-Key: $ADPILOT_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"question":"Which campaign has the worst cost per acquisition?"}'
```

Or in a container:

```bash
docker build -t adpilot . && docker run --rm -p 8080:8080 \
  -e ADPILOT_API_KEY=$ADPILOT_API_KEY -e ADPILOT_AUDIT=memory -e ADPILOT_CONNECTOR=duckdb adpilot
```

See [docs/api.md](docs/api.md) for auth, `/ask/stream` (SSE) and the two request-size tiers, and
[docs/deploy.md](docs/deploy.md) for Cloud Run.

Pipeline (needs a GCP project with BigQuery and `gcloud auth application-default login`):

```bash
python pipelines/01_validate_and_ingest.py --all
python pipelines/02_run_transformations.py
python pipelines/03_anomaly_detection.py
python pipelines/04_budget_optimizer.py
python pipelines/05_forecast.py
python pipelines/07_qa_validation.py
adpilot --connector bigquery chat
```

Dashboard:

```bash
cd streamlit_app && streamlit run Home.py
```

Tests (deterministic — the model is scripted with pydantic-ai's `TestModel`/`FunctionModel`, the data is DuckDB):

```bash
ruff check . && pytest -q
```

### Evals

```bash
adpilot eval                       # deterministic tier, no API key needed
adpilot eval --check-cases         # verify golden values against the sample data
adpilot eval --tier model          # real models; writes evals/reports/ and the badge
adpilot audit runs                 # recent eval runs from the BigQuery audit trail
```

See [docs/evals.md](docs/evals.md) for how scoring works and what the nightly PR means, and [docs/security.md](docs/security.md) for the guardrail layers and what each one can and cannot catch. Every agent call is recorded in BigQuery (tokens, cost, latency, model used); see [docs/observability.md](docs/observability.md).

## Daily brief

```bash
adpilot brief --out brief.md
```

The agent answers the pack's `briefing.questions` (`packs/ads/pack.yaml`) exactly as it answers a chat question. Then one tool-less model call turns the answers into **what changed**, **so what** and **what to do**, citing the question and trace behind each finding. Answers that refused or fell back to a canned query are listed as *Unavailable*, never presented as findings. Any number in the brief that doesn't appear in the answers is flagged `BriefUngrounded`. Without a model key you get the raw answers.

`.github/workflows/brief-daily.yml` runs it every day at 12:00 UTC and opens a GitHub issue titled `Daily brief YYYY-MM-DD`. Every question and the synthesis are in the audit trail under one run id: `adpilot audit export --run <id>`.

The bundled data set covers January 2024, so until the data refreshes the brief reports the same period each day.

## Configuration

All names and keys come from `.env` (local) or `secrets.toml` (Streamlit Cloud). See `.env.example`. The connector is picked by `--connector`, `ADPILOT_CONNECTOR`, or the pack default.

The agent and the eval judge have separate chains, so the agent's model never grades its own answers. Each variable is read under one name only — a legacy spelling is ignored with a warning, never silently aliased.

- `AGENT_LLM_BEARER_TOKEN` — OpenRouter key for the analyst agent (required for model answers; without it the CLI answers from pre-defined queries).
- `AGENT_LLM_TARGET_MODEL` / `AGENT_LLM_FALLBACK_MODEL` — the agent's chain. Defaults are OpenRouter free-tier models; an empty fallback means "primary only".
- `JUDGE_LLM_BEARER_TOKEN` — key for the judge; falls back to the agent's key.
- `JUDGE_LLM_TARGET_MODEL` / `JUDGE_LLM_FALLBACK_MODEL` / `JUDGE_LLM_ENDPOINT_URL` — the judge's chain and, optionally, a different OpenAI-compatible provider.

- `GCP_SERVICE_ACCOUNT_JSON` / `GOOGLE_APPLICATION_CREDENTIALS` — BigQuery credentials for the audit trail (service-account JSON, or a key file path / ADC).
- `BQ_PROJECT_ID` — GCP project holding the audit dataset (required; no default).
- `BQ_AUDIT_DATASET` — audit dataset name (default `adpilot_audit`).
- `LOGFIRE_TOKEN` — optional; turns on live OpenTelemetry traces for agent and eval calls.
- `ADPILOT_AUDIT` — set to `memory` to run unrecorded (chat/eval otherwise refuse to start without BigQuery credentials).
- `ADPILOT_API_KEY` — required to serve the HTTP API; ≥24 characters, or `create_app` refuses to start.
- `ADPILOT_API_RPM` — API rate limit, requests/minute (default 20). See [docs/api.md](docs/api.md).

## Architecture

```
                 packs/ads/  (allowlist · glossary · prompt · fallback queries · duckdb_setup.sql)
                        │
 question ─► input gate ─► Agent (Pydantic AI) ─► tools ─► SQL validator ─► connector ─► DuckDB | BigQuery
             scope/inject/    │  primary → fallback → rule-based      ▲                      │
             SQL shapes       └────── SqlError{kind, hint, columns} ◄─┘  (self-heal loop)    ▼
                                                                                     AnalystAnswer ─► output rail
                                                                       {answer_md, sql, data, chart, confidence, caveats}

 data/raw/*.csv ─► 01 ingest ─► Bronze ─► 02 transform ─► Gold mart ─► 03 anomalies · 04 optimizer · 05 forecast · 07 QA
```

## History

Started January 2026. The first iteration was lost to a drive failure and rebuilt from June 2026 onward; the agentic rebuild in the roadmap is the current focus.

## License

MIT
