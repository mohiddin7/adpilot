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

The image installs `.[api,mcp]`, so the same container serves the MCP tools at `/mcp` behind the API key
(see [mcp.md](mcp.md)); nothing else to deploy.

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

## Daily data (Phase 3D)

**No GCP resource is created by this branch.** Everything below is documentation for a later,
owner-approved rollout. Each setup command needs the owner's explicit yes for that specific step — show the
command, say what it creates and what it costs, wait, run, then show the result — exactly like the Cloud Run
section above.

### What runs, and when

```
Cloud Scheduler "0 2 * * *" Etc/UTC ──OIDC──▶ generate_daily   (HTTP, IAM-only)
   dates = [min(max(gold.date) + 1, today − 3) … today − 1], as_of = today UTC
   refuse: gold empty, or gap > 31 days  → "run the backfill"
   write  landing/<as_of>/{facebook,google,tiktok}_ads.csv, then _manifest.json LAST
                                  │ object.finalized (Eventarc, retries OFF)
                                  ▼
run_pipeline (event fn, max-instances 1, timeout 540 s)
   ignore unless name starts "landing/" and ends "_manifest.json"
   download the listed CSVs to /tmp → 01 ×3 → 02 → 03 → 04 → 05 → 07
   write landing/<as_of>/_status.json {ok, run_id, steps, planted, flagged, error}
```

Both entry points live in `pipelines/main.py`, deployed twice from `--source pipelines` with
`pipelines/requirements.txt` (the pipeline subset of the root file plus `functions-framework`). Retries stay
off on purpose: recovery is catch-up on the next run, never a redelivery of a stale batch. Project, dataset
and bucket names never enter the repo — everything below reads them from `$BQ_PROJECT_ID`,
`$BQ_BRONZE_DATASET`, `$BQ_STAGING_DATASET`, `$BQ_PRODUCTION_DATASET` and `$RAW_BUCKET`.

### Prerequisites

- `RAW_BUCKET` is in `.env` alongside the existing `BQ_*` variables.
- Load them into the current shell before running anything below:

```bash
set -a; source .env; set +a
```

### One-time setup (owner-run, one approval per step)

```bash
REGION=us-east4
SA=adpilot-pipeline@${BQ_PROJECT_ID}.iam.gserviceaccount.com
```

**Zsh quoting:** always write `"${VAR}:dataset"` with braces — `"$VAR:dataset"` triggers zsh's `:a` history
modifier and silently corrupts the argument. This matters below wherever a `bq` command addresses a table as
`project:dataset.table`.

1. **Enable the APIs** (owner yes):

   ```bash
   gcloud services enable cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
     artifactregistry.googleapis.com eventarc.googleapis.com pubsub.googleapis.com cloudscheduler.googleapis.com \
     --project "$BQ_PROJECT_ID"
   ```

2. **Service account and least-privilege grants** (owner yes):

   ```bash
   gcloud iam service-accounts create adpilot-pipeline --display-name "AdPilot daily data" --project "$BQ_PROJECT_ID"
   gcloud projects add-iam-policy-binding "$BQ_PROJECT_ID" --member "serviceAccount:${SA}" --role roles/bigquery.jobUser
   gcloud projects add-iam-policy-binding "$BQ_PROJECT_ID" --member "serviceAccount:${SA}" --role roles/eventarc.eventReceiver
   for DS in "$BQ_BRONZE_DATASET" "$BQ_STAGING_DATASET" "$BQ_PRODUCTION_DATASET"; do
     bq query --use_legacy_sql=false --project_id "$BQ_PROJECT_ID" \
       "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`${BQ_PROJECT_ID}.${DS}\` TO 'serviceAccount:${SA}'"
   done
   gcloud storage buckets add-iam-policy-binding "gs://${RAW_BUCKET}" --member "serviceAccount:${SA}" --role roles/storage.objectAdmin
   GCS_AGENT=$(gcloud storage service-agent --project "$BQ_PROJECT_ID")
   gcloud projects add-iam-policy-binding "$BQ_PROJECT_ID" --member "serviceAccount:${GCS_AGENT}" --role roles/pubsub.publisher
   ```

   Negative check: impersonating the SA, a query on `adpilot_audit` must fail with 403.

3. **Cluster bronze** (owner yes; metadata only, no rebuild):

   ```bash
   for T in facebook_ads_landing google_ads_landing tiktok_ads_landing; do
     bq update --clustering_fields=date,campaign_id "${BQ_PROJECT_ID}:${BQ_BRONZE_DATASET}.${T}"
   done
   ```

4. **Deploy both functions** (owner yes):

   ```bash
   ENV="BQ_PROJECT_ID=${BQ_PROJECT_ID},BQ_BRONZE_DATASET=${BQ_BRONZE_DATASET},BQ_STAGING_DATASET=${BQ_STAGING_DATASET},BQ_PRODUCTION_DATASET=${BQ_PRODUCTION_DATASET},RAW_BUCKET=${RAW_BUCKET}"
   gcloud functions deploy run-pipeline --gen2 --region "$REGION" --runtime python312 --source pipelines \
     --entry-point run_pipeline --trigger-event-filters "type=google.cloud.storage.object.v1.finalized" \
     --trigger-event-filters "bucket=${RAW_BUCKET}" --trigger-location "$REGION" \
     --service-account "$SA" --trigger-service-account "$SA" --set-env-vars "$ENV" \
     --memory 2Gi --cpu 1 --timeout 540s --max-instances 1 --concurrency 1 --no-retry --project "$BQ_PROJECT_ID"
   gcloud functions deploy generate-daily --gen2 --region "$REGION" --runtime python312 --source pipelines \
     --entry-point generate_daily --trigger-http --no-allow-unauthenticated \
     --service-account "$SA" --set-env-vars "$ENV" --memory 1Gi --timeout 300s --max-instances 1 --project "$BQ_PROJECT_ID"
   for F in generate-daily run-pipeline; do
     gcloud functions add-invoker-policy-binding "$F" --region "$REGION" --member "serviceAccount:${SA}" --project "$BQ_PROJECT_ID"
   done
   ```

5. **Backfill and verify** (owner yes). Run `python pipelines/main.py backfill`, then wait for `_status.json`:

   ```bash
   gcloud storage cat "gs://${RAW_BUCKET}/landing/$(date -u +%F)/_status.json"
   bq query --use_legacy_sql=false "SELECT MIN(date), MAX(date), COUNT(*) FROM \`${BQ_PROJECT_ID}.${BQ_PRODUCTION_DATASET}.fct_unified_marketing_performance\`"
   ```

   Expected: `ok: true`; gold spans 2024-01-01 … yesterday; `planted_flagged / planted` is in line with the
   pinned recall. **Record the total `seconds`.** This backfill is the largest run the function will ever
   see, so its `_status.json` `seconds` is the timeout measurement — if it exceeds 400, stop and report: the
   daily path needs the Cloud Run job named in spec §10 before scheduling. Otherwise, continue and check:
   - re-triggering the same manifest (download and re-upload it) leaves gold's row count and spend total
     unchanged;
   - `tbl_pipeline_runs` shows one row per step with non-zero `jobs`;
   - `JOBS_BY_USER` labels show the `02_transform` MERGE jobs.

6. **Schedule** (owner yes):

   ```bash
   URL=$(gcloud functions describe generate-daily --gen2 --region "$REGION" --format='value(serviceConfig.uri)' --project "$BQ_PROJECT_ID")
   gcloud scheduler jobs create http adpilot-daily-data --location "$REGION" --schedule "0 2 * * *" --time-zone "Etc/UTC" \
     --uri "$URL" --http-method POST --oidc-service-account-email "$SA" --oidc-token-audience "$URL" \
     --attempt-deadline 320s --project "$BQ_PROJECT_ID"
   ```

   The next morning, after 02:15 UTC: today's `_status.json` shows `ok: true`, gold `MAX(date)` = yesterday,
   and the daily 01/02 steps bill at the 10 MB minimum.

### Backfill

Re-running the initial load (or recovering from a gap larger than the daily catch-up window) runs locally
with owner credentials, exactly like a daily batch:

```bash
python pipelines/main.py backfill
```

### Replay a batch

Batches are never moved or deleted, so a replay is always available:

- download `landing/<as_of>/_manifest.json`, then re-upload it unchanged (a new `finalized` event re-runs
  the same batch through the deployed function), or
- run it locally with the same code:

  ```bash
  python pipelines/main.py run gs://$RAW_BUCKET/landing/<as_of>/_manifest.json
  ```

Both are safe: the MERGEs are idempotent, so a duplicate delivery of the same manifest is a no-op.

**`_status.json` is overwritten, not appended.** A replay writes a fresh `run_id`, `steps` and `error` to
`landing/<as_of>/_status.json`, so replaying after a failure destroys that failed run's record at this path.
If you need it, copy it first:

```bash
gcloud storage cp gs://$RAW_BUCKET/landing/<as_of>/_status.json ./status-<as_of>-before-replay.json
```

`tbl_pipeline_runs` is append-only and keeps every run's rows, failed or not, so the history survives there
regardless.

### Status

```bash
gcloud storage cat gs://$RAW_BUCKET/landing/$(date -u +%F)/_status.json
```

### Cost and latency per step

```sql
SELECT batch, step, seconds, bytes_billed, slot_ms, cache_hits, ok, error
FROM `$BQ_PROJECT_ID.$BQ_STAGING_DATASET.tbl_pipeline_runs`
WHERE started_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
ORDER BY started_at DESC, step
```

`_status.json`'s own `steps` array carries the same per-step numbers (`jobs`, `bytes_billed`, `slot_ms`,
`cache_hits`), so a single batch can be inspected without a query.

### SLO

Gold is complete through yesterday UTC by 03:00 UTC. The brief's freshness gate fails its workflow when it
is not.
