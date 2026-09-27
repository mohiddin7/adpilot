# AdPilot

![ci](https://github.com/mohiddin7/adpilot/actions/workflows/ci.yml/badge.svg) ![evals](https://img.shields.io/badge/evals-90%25-brightgreen)

**An agentic analytics platform for marketing data.** Unifies multi-channel ad performance in a BigQuery lakehouse, enriches it with anomaly detection, forecasting and budget optimization, and puts an AI analyst on top that answers questions in plain English — with guardrails, self-healing SQL and an evaluation harness.

> Status: active rebuild. The agent core now runs on Pydantic AI with a data-agnostic "pack" system. The FastAPI service, the daily brief and the MCP server are live. See the roadmap below.

## What it does today

- **Agent** (`adpilot/`): one Pydantic AI agent with typed tools (`run_sql`, `get_anomalies`, `get_forecast`, `get_budget_plan`, `render_chart`) over any SQL source. Model-written SQL goes through a defense-in-depth validator; data-source errors are returned to the model as structured hints so it repairs its own query (max 3 executions, 4 model calls per question). Primary → fallback model chain on 429s, then a deterministic rule-based answer so the user never sees a stack trace.
- **Packs** (`packs/ads/`): everything domain-specific — table allowlist, column descriptions, glossary, system prompt, canned fallback queries and a DuckDB bootstrap. Swap the directory to point the same agent at different data.
- **Connectors**: BigQuery (bytes-billed cap) and DuckDB over the raw CSVs, so the agent and tests run with zero credentials.
- **Pipeline** (`pipelines/`, daily at 02:00 UTC on Cloud Run functions — see docs/deploy.md): calibrated synthetic source → validate → Bronze MERGE → Gold MERGE (30-column contract) → anomaly flags (MAD z-score) → budget optimizer (LP) → 14-day forecast (Holt-Winters) → QA reconciliation. Idempotent, audited, cost-capped.
- **Dashboard** (`streamlit_app/`): pack-driven overview, channel deep dive with every filter the pack declares, AI insight cards, and a chat on every page (with a "show thinking" toggle). It holds no database or model credentials: every number and answer comes from the API (`/dashboard`, `/filters`, `/panels`, `/pacing`, `/ask`).

## Roadmap

| Phase | Deliverable |
|---|---|
| 0 | Repo hygiene, env-driven config, CI ✅ |
| 1 | Data-agnostic agent core (Pydantic AI), typed tools, guardrails, `adpilot chat` CLI ✅ |
| 2 | Eval harness: golden cases, red-team, self-heal rate, LLM judge, scorecard ✅ |
| 3 | Three surfaces over the same agent: FastAPI service ✅ · daily brief ✅ · MCP server ✅ |
| 4 | Pack-driven dashboard ✅ · human-in-the-loop budget approvals |
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

## MCP

Claude Desktop and Claude Code can use the agent as two MCP tools, `ask` and `schema`:

```bash
pip install -e ".[mcp]"
claude mcp add adpilot -e ADPILOT_AUDIT=memory -- "$(pwd)/.venv/bin/adpilot" --connector duckdb mcp
```

The deployed API serves the same tools at `/mcp` behind `X-API-Key`. See [docs/mcp.md](docs/mcp.md).

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

The dashboard is a client of the API: start the API (above), or set `ADPILOT_API_URL` to the deployed service. It
reads `ADPILOT_API_URL` and `ADPILOT_API_KEY` from the environment / `.env`, or from an `[api]` section in Streamlit
secrets.

```bash
streamlit run streamlit_app/Home.py
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

A short list of decisions, not a wall of numbers. It opens with how many things need you today, then up to three items ranked by dollars at stake. Each item says what happened, what was checked, what to do, how sure it is, and what the next brief will check.

What it does that a chart doesn't:

- **Explains cost changes.** A platform's cost per sale splits exactly into ad price, clicks per view and sales per click, so the brief names the cause (auction pressure, tired ads, or a landing-page / tracking problem) and the fix that goes with it.
- **Sorts out anomaly noise.** It finds broken tracking (clicks normal, sales down by half or more) and double counting (sales up 3× on normal clicks) even when the detector didn't flag them, and folds small one-day flags into one line.
- **Looks ahead.** Month-end spend against the monthly budgets in `packs/ads/pack.yaml` (`briefing.budgets`), and where the 14-day forecast says cost per sale is heading. A big optimizer move becomes a staged test with a stop rule.
- **Follows up.** Every published item is stored in the audit dataset (`brief_items`), and the next brief reports whether it was resolved, looks done, is still open, or expired.

Code computes and formats every number from four fixed read-only queries; nothing goes through the agent. One tool-less model call only words the headline, a short story and each item's title, cause and action. Each piece that cites a number not in its facts falls back to a template, so a failed or rate-limited model still gives a complete, correct brief. The newest two days are left out of cost analyses because their sales are still arriving. Thresholds live in `briefing.thresholds`; an unknown key stops the brief rather than being ignored. The brief is redacted and its campaign names escaped before it's published.

`.github/workflows/brief-daily.yml` runs it every day at 12:00 UTC and opens a GitHub issue titled `Daily brief YYYY-MM-DD`. The job fails (after publishing) when an input table or the follow-up memory could not be read. The writer call is in the audit trail under the brief's run id: `adpilot audit export --run <id>`.

## Configuration

All names and keys come from `.env` (see `.env.example`) or, for the deployed API, its Cloud Run environment. The Streamlit Cloud secrets hold only the dashboard's `[api]` section (`ADPILOT_API_URL`, `ADPILOT_API_KEY`). The connector is picked by `--connector`, `ADPILOT_CONNECTOR`, or the pack default.

The agent and the eval judge have separate chains, so the agent's model never grades its own answers. Each variable is read under one name only — a legacy spelling is ignored with a warning, never silently aliased.

- `AGENT_LLM_BEARER_TOKEN` — OpenRouter key for the analyst agent (required for model answers; without it the CLI answers from pre-defined queries).
- `AGENT_LLM_MODELS` — the agent's chain, a comma-separated list tried in order (`a/x:free,b/y:free,openrouter/free`). Unset or empty → the default free chain in code. See [docs/models.md](docs/models.md).
- `JUDGE_LLM_BEARER_TOKEN` — key for the judge; falls back to the agent's key.
- `JUDGE_LLM_MODELS` / `JUDGE_LLM_ENDPOINT_URL` — the judge's chain (same format) and, optionally, a different OpenAI-compatible provider. The eval refuses to start if the two chains share a model other than `openrouter/free`.

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
