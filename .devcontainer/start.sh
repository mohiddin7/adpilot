#!/usr/bin/env bash
# Codespaces: the API on the bundled DuckDB sample data, and the dashboard that calls it. Nothing is recorded.
# Add AGENT_LLM_BEARER_TOKEN as a Codespaces secret for model answers; without it, chat answers from pre-defined queries.
set -euo pipefail
export ADPILOT_CONNECTOR=duckdb ADPILOT_AUDIT=memory ADPILOT_API_URL=http://localhost:8080
export ADPILOT_API_KEY="${ADPILOT_API_KEY:-$(python -c 'import secrets; print(secrets.token_hex(16))')}"
uvicorn --factory adpilot.api.app:create_app --workers 1 --port 8080 > /tmp/adpilot-api.log 2>&1 &
exec streamlit run streamlit_app/Home.py --server.headless true \
  --server.enableCORS false --server.enableXsrfProtection false
