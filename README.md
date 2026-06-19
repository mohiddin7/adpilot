# AdPilot

**An agentic analytics platform for marketing data.** Unifies multi-channel ad performance in a BigQuery lakehouse, enriches it with anomaly detection, forecasting and budget optimization, and puts an AI analyst on top that answers questions in plain English — with guardrails, self-healing SQL and an evaluation harness.

> Status: active rebuild. The agent core is being re-architected on Pydantic AI with a data-agnostic "pack" system, a FastAPI service, a proactive briefing agent and an MCP server. See the roadmap below.

## What it does today

- **Pipeline** (`pipelines/`): validate → Bronze MERGE → Gold MERGE (30-column contract) → anomaly flags (MAD z-score) → budget optimizer (LP) → 14-day forecast (Holt-Winters) → QA reconciliation. Idempotent, audited, cost-capped.
- **Dashboard** (`streamlit_app/`): performance overview, per-channel deep dives, AI insight cards, and a chat that turns questions into validated BigQuery SQL and explains the result.

## Roadmap

| Phase | Deliverable |
|---|---|
| 0 | Repo hygiene, env-driven config, CI ✅ |
| 1 | Data-agnostic agent core (Pydantic AI), typed tools, guardrails, `adpilot chat` CLI |
| 2 | Eval harness: golden cases, red-team, self-heal rate, LLM judge, scorecard |
| 3 | FastAPI streaming API + generic dashboard driven by pack config |
| 4 | Proactive briefing agent + human-in-the-loop budget approvals |
| 5 | MCP server, tracing, Docker |

## Quick start

```bash
git clone https://github.com/mohiddin7/adpilot.git && cd adpilot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in BigQuery + LLM values
```

Pipeline (needs a GCP project with BigQuery and `gcloud auth application-default login`):

```bash
python pipelines/01_validate_and_ingest.py --all
python pipelines/02_run_transformations.py
python pipelines/03_anomaly_detection.py
python pipelines/04_budget_optimizer.py
python pipelines/05_forecast.py
python pipelines/07_qa_validation.py
```

Dashboard:

```bash
cd streamlit_app && streamlit run Home.py
```

Tests:

```bash
pytest -q
```

## Configuration

All names and keys come from `.env` (local) or `secrets.toml` (Streamlit Cloud). See `.env.example`. The LLM layer speaks the OpenAI chat-completions format, so any provider works — OpenRouter free models are the default.

## Architecture (current)

```
data/raw/*.csv ──► 01 validate+ingest ──► Bronze (all-STRING landing)
                          │
                          ▼
                   02 transformations ──► Gold mart (30-col contract) + quarantine
                          │
            ┌─────────────┼─────────────┐
            ▼             ▼             ▼
      03 anomalies   04 optimizer   05 forecast      07 QA reconciliation
            └─────────────┴─────────────┘
                          ▼
                  Streamlit dashboard + SQL chat agent
```

## History

Started January 2026. The first iteration was lost to a drive failure and rebuilt from June 2026 onward; the agentic rebuild in the roadmap is the current focus.

## License

MIT
