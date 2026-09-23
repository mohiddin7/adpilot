# Observability and the audit trail

Every call the agent answers — from `adpilot chat`, from an eval run, and every LLM-judge call — is recorded
in BigQuery. Grades attach to those records as separate rows, so a case can be re-judged or hand-labelled
later without rewriting history. Live traces (optional) go to Logfire.

## What is recorded

Dataset `adpilot_audit` (name from `BQ_AUDIT_DATASET`), three tables, all partitioned by day and append-only:

| table | one row per | key columns |
|---|---|---|
| `agent_calls` | agent or judge call | `trace_id`, `ts`, `source` (chat/eval/judge/api; `mcp` and `brief` are coming), `session_id`, `run_id`, `case_name`, `question`, `answer_md`, `sql`, `model_requested`, `model_used`, `fell_back`, `tokens_in/out`, `cost_usd`, `latency_s`, `refused`, `error_kind`, `messages_json` |
| `scores` | grade on a call | `trace_id`, `run_id`, `name` (`factual`, `safe_sql`, `judge`, …), `value`, `passed`, `source` (code/judge/human), `grader`, `reason` |
| `eval_runs` | `adpilot eval` run | `run_id`, `tier`, `overall`, `gate_ok`, `gate_reasons`, `scorecard_json`, `calls_used`, `tokens_in/out`, `cost_usd` |

Every table also has `attributes` (JSON text, for anything new) and `schema_version`. The code creates the
dataset and tables and adds missing columns itself; it never renames or drops one.

## Setup (once)

1. Give the service account `roles/bigquery.jobUser` on the project (it needs to create query and load jobs),
   and write access to the audit dataset. Least privilege — create the dataset yourself and grant the service
   account `WRITER` on that dataset only, so it gains no access to your other data:

   ```bash
   bq --location=US mk --dataset <project>:adpilot_audit
   # then add {"role": "WRITER", "userByEmail": "<sa>@<project>.iam.gserviceaccount.com"}
   # to the dataset's access[] via: bq show --format=prettyjson … > ds.json && bq update --source ds.json …
   ```

   Simpler but broader: `roles/bigquery.dataEditor` on the whole project lets `preflight` create the dataset
   itself, at the cost of write access to every dataset in the project.
2. Locally: `GOOGLE_APPLICATION_CREDENTIALS=<key path>` (or `GCP_SERVICE_ACCOUNT_JSON=<json>`) and `BQ_PROJECT_ID`
   in `.env`. Optional: `BQ_AUDIT_DATASET` (default `adpilot_audit`), `BQ_LOCATION` (default: same as the staging dataset, or `US` if there is none).
3. GitHub Actions: secrets `GCP_SERVICE_ACCOUNT_JSON` and `BQ_PROJECT_ID` (the nightly workflow reads both).
4. `adpilot audit preflight` — verifies credentials and creates/updates the tables. It prints the fix if anything is missing.

Opt out only explicitly: `ADPILOT_AUDIT=memory` runs without recording and says so on every start. There is no
silent fallback — if the store is unreachable, `chat` and `eval` refuse to start (exit 2).

## Failure policy

Strict at startup, tolerant mid-run: a write that fails gets 3 attempts total (sleeping 1s, then 4s, between them) and is kept in memory if all three fail; at
the end `eval` exits 2 if rows are still unpersisted (the reports are still written), `chat` prints a warning
and still shows the answer.

## Live traces (optional)

`pip install 'adpilot[logfire]'`, create a Logfire project, set `LOGFIRE_TOKEN`. pydantic-ai and pydantic-evals
then emit OpenTelemetry spans for every model and tool call; `agent_calls.otel_trace_id` links a BigQuery row to
its trace. Without the token nothing changes.

## CLI

```bash
adpilot audit preflight                 # check creds/dataset/tables; creates or alters as needed
adpilot audit runs --limit 20           # recent eval runs: overall, gate, calls, cost
adpilot audit export --run <run_id>     # one run's calls with their scores, JSONL (add --csv for a spreadsheet)
```

The nightly workflow uploads the export as a 90-day artifact; BigQuery is the permanent copy.

## Useful queries

Cost per run:

```sql
SELECT run_id, ts, overall, calls_used, tokens_in, tokens_out, cost_usd
FROM `adpilot_audit.eval_runs` ORDER BY ts DESC LIMIT 30;
```

Cases where the code grader and the judge disagree:

```sql
SELECT c.run_id, c.case_name, f.passed AS factual, j.value AS judge
FROM `adpilot_audit.agent_calls` c
JOIN `adpilot_audit.scores` f ON f.trace_id = c.trace_id AND f.name = 'factual'
JOIN `adpilot_audit.scores` j ON j.trace_id = c.trace_id AND j.name = 'judge_rescued'
WHERE f.passed = FALSE AND j.value = 1.0;
```

Fallback rate by day:

```sql
SELECT DATE(ts) AS day, COUNTIF(fell_back) / COUNT(*) AS fallback_rate, COUNT(*) AS calls
FROM `adpilot_audit.agent_calls` WHERE source != 'judge' GROUP BY day ORDER BY day DESC;
```

Re-grade a run later: insert new `scores` rows with the same `trace_id`, a new `grader`, and `source = 'judge'`
(or `'human'`); nothing else changes.
