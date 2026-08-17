# Deploying the API

**No GCP resource is created by this branch.** Everything below is documentation for a later,
owner-approved step — the container runs and is smoke-tested locally, and deploying it is a one-command
follow-up, not something this branch does on its own.

## Container

```bash
docker build -t adpilot .
docker run --rm -d -p 8080:8080 \
  -e ADPILOT_API_KEY=$(python -c "import secrets;print(secrets.token_hex(16))") \
  -e ADPILOT_AUDIT=memory \
  -e ADPILOT_CONNECTOR=duckdb \
  --name adpilot-smoke adpilot
curl -s localhost:8080/healthz   # {"status":"ok"}
docker rm -f adpilot-smoke
```

`data/raw/*.csv` stays in the image on purpose (see the Dockerfile comment) so it runs credential-free in
DuckDB mode for a demo — a deliberate size trade, not an oversight. For BigQuery, drop the
`-e ADPILOT_CONNECTOR` line and mount or inject credentials instead (`GOOGLE_APPLICATION_CREDENTIALS` or
`GCP_SERVICE_ACCOUNT_JSON` — see [observability.md](observability.md)).

`--workers 1` is baked into the image's `CMD` and is load-bearing, not a default to bump: see
[api.md](api.md#one-worker) for why. Scale by running more container instances, never by raising
`--workers`.

## Cloud Run (documentation only — do not run without owner sign-off)

```bash
gcloud run deploy adpilot-api \
  --image gcr.io/${PROJ}/adpilot \
  --region us-central1 \
  --min-instances 0 --max-instances 2 \
  --concurrency 4 \
  --no-cpu-throttling \
  --timeout 300 \
  --port 8080 \
  --service-account adpilot-api@${PROJ}.iam.gserviceaccount.com \
  --set-secrets ADPILOT_API_KEY=adpilot-api-key:latest,AGENT_LLM_BEARER_TOKEN=agent-llm-bearer-token:latest
```

`--no-cpu-throttling` is not optional: audit rows are flushed in a background task *after* the response, and
Cloud Run's default throttles CPU the moment a response completes, so without it the last rows of an instance
can be lost. The shutdown flush covers the rest.

`--workers 1` is already in the image's `CMD`; nothing on the `gcloud run deploy` line needs to repeat it.
`--region` should match the BigQuery dataset's location (`BQ_LOCATION`, default `US`) to avoid cross-region
query cost and latency.

**Zsh quoting:** always write `"${PROJ}:dataset"` — `"$PROJ:adpilot_audit"` triggers zsh's `:a` history
modifier and silently corrupts the argument.

Do not set `GCP_SERVICE_ACCOUNT_JSON` in the Cloud Run environment. Credentials come from the attached
service account's ambient identity (Application Default Credentials); the env var is a local/CI-only escape
hatch for when there is no attached identity to use.

### Identity and roles

One service account, `adpilot-api@${PROJ}.iam.gserviceaccount.com`, attached to the Cloud Run service:

| Role | Scope | Why |
|---|---|---|
| `roles/bigquery.jobUser` | project | run query and load jobs |
| `roles/bigquery.dataEditor` | `adpilot_audit` dataset only | write `agent_calls`/`scores`/`eval_runs` |
| `roles/bigquery.dataViewer` | the production dataset | read marketing data for `run_sql` |

Same least-privilege pattern as the audit service accounts in [observability.md](observability.md): no
project-wide `dataEditor`, so the API's credentials cannot touch data outside these two grants.

### Secrets

Two Secret Manager secrets, referenced with `--set-secrets` above, never with `--set-env-vars`:

- `adpilot-api-key` — the value `ADPILOT_API_KEY` resolves to. ≥24 characters (`create_app` refuses to start
  otherwise).
- `agent-llm-bearer-token` — the value `AGENT_LLM_BEARER_TOKEN` resolves to.

### Rotating the API key

1. `python -c "import secrets;print(secrets.token_hex(16))"` for a new value.
2. `gcloud secrets versions add adpilot-api-key --data-file=-` (paste the value, `Ctrl-D`).
3. Redeploy (or `gcloud run services update adpilot-api --update-secrets
   ADPILOT_API_KEY=adpilot-api-key:latest`) so the revision picks up `:latest`.
4. Update callers, then destroy the old secret version once nothing uses it.

There is no dual-key grace period — swapping the secret rotates instantly for every request the new
revision serves. Roll out gradually with Cloud Run traffic splitting if that matters.
