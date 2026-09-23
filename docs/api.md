# HTTP API

A thin transport over the same `ask()` the CLI uses: one guard chain, one model-fallback chain, one audit
record, whichever surface calls it. `adpilot/api/app.py` builds a FastAPI app via a factory — there is no
module-level `app`, so uvicorn needs `--factory`.

## Run it

```bash
export ADPILOT_API_KEY=$(python -c "import secrets;print(secrets.token_hex(16))")
uvicorn --factory adpilot.api.app:create_app --workers 1
```

The process refuses to start without `ADPILOT_API_KEY` set to at least 24 characters, and refuses to start
if the audit store is unreachable (`ADPILOT_AUDIT=memory` to run unrecorded — see
[observability.md](observability.md)). Same fail-closed behaviour as `adpilot chat`, just enforced at process
start instead of on first use.

Every non-`/healthz` route requires `X-API-Key: <key>`, checked with a constant-time comparison. A wrong or
missing key is a 401 and writes no audit row — only a question that actually reaches `ask()` gets recorded.
`/openapi.json`, `/docs` and `/redoc` are disabled (`404`) rather than left open: FastAPI serves them without
a dependency, so an unauthenticated caller could otherwise enumerate every route and request schema.

## Configuration

- `ADPILOT_API_KEY` — required, ≥24 characters, compared with `secrets.compare_digest`.
- `ADPILOT_API_RPM` — requests/minute shared by `/ask` and `/ask/stream` (default 20), enforced by
  `app.state.limiter`. Over the limit: `429` with a `Retry-After` header, and the request never reaches
  `ask()` — nothing is audited. This is separate from `MODEL_RATE_LIMITER`, a fixed 20 rpm bucket inside
  `adpilot/core/models.py` that throttles the actual model calls `ask()` makes and is not configurable by
  `ADPILOT_API_RPM` — see [One worker](#one-worker).

## Endpoints

| Method | Path | Auth | Returns |
|---|---|---|---|
| GET | `/healthz` | none | `{"status": "ok"}` — process liveness only, nothing about pack/connector/model |
| GET | `/schema` | key | plain-text schema summary the agent sees |
| POST | `/ask` | key | `AnswerBody` JSON, once the answer is ready |
| GET | `/ask/stream` | key | `text/event-stream` — progress events, then the same `AnswerBody` |

## `POST /ask`

```bash
curl -sX POST localhost:8000/ask \
  -H "X-API-Key: $ADPILOT_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"question":"Which campaign has the worst cost per acquisition?"}'
```

Body: `question` (1–4000 chars) and optional `session_id` (`[A-Za-z0-9._-]{1,64}`, ties the turn to a
continuable session for history). `extra="forbid"` — a `connector` field is rejected, not ignored: the data
source is server-side configuration, never something a caller repoints.

Response is `AnswerBody`: `trace_id`, `answer_md`, `sql`, `data`, `chart`, `confidence`, `caveats`, `refused`.

## `GET /ask/stream`

Same question/session_id as query params, `text/event-stream` response. A raw browser `EventSource` cannot
be used here — it has no way to set `X-API-Key`, and the key does not go in the query string (it would land
in access logs and browser history). Use `fetch` or `httpx`:

```bash
curl -N -sG localhost:8000/ask/stream \
  -H "X-API-Key: $ADPILOT_API_KEY" \
  --data-urlencode "question=Which campaign has the worst cost per acquisition?"
```

Event types:

| event | when | payload |
|---|---|---|
| `status` | first, then once per tool call/repair/final-result | `{"phase", "tool", "detail"}` |
| `answer` | once, before `done` | the same `AnswerBody` as `POST /ask` |
| `error` | only on a transport/threading failure — `ask()`'s own failures already come back as a refused `answer` | `{"kind", "message"}` |
| `done` | always last | `{}` |

`phase` is one of `thinking | tool | sql | repair | answering`. `thinking` is emitted immediately, before the
run starts, so a client shows progress even on the rule-based path that never calls a model. `tool`/`sql`
name the tool call (`run_sql` gets `phase: "sql"` and a truncated `detail` of the query text); `repair` fires
when the model is about to retry a failed query; `answering` fires on the model's final result.

**SSE does not take the same provider code path as `POST /ask`.** Passing an event-stream handler makes
pydantic-ai use its streaming request path (`request_stream`) instead of `request`. It is still one `ask()`,
one guard chain, one audit record — but the model call itself differs, so a provider that streams badly will
show it on `/ask/stream` and not on `/ask`. If the two diverge in testing, that is where to look first.

**A client that disconnects mid-stream does not cancel the run.** `ask()` runs in an OS thread
(`anyio.to_thread.run_sync`); cancelling an `await` cannot stop a thread that is already running, so the question
completes and its audit row is written regardless. This is intended: a caller cannot dodge being recorded by
hanging up, and a disconnect still costs whatever model tokens the run used. Pinned by
`test_a_client_disconnect_still_records_the_run` in `tests/test_api.py`.

## The two size tiers

Two independent caps, checked at two different layers, with two different consequences:

| Cap | Layer | Over the limit | Audited? |
|---|---|---|---|
| 4000 chars | transport (`AskRequest.question`, pydantic) | `422` | no — rejected before `ask()` |
| 600 chars | `sanitize_question` inside `ask()` (semantic input gate) | `200` refusal (`InputPolicy` in `caveats`) | yes |

A body under 4000 but over 600 chars passes transport validation and is refused by the guard — that refusal
is a real `ask()` outcome, so it is recorded like any other call.

## Why a refusal is a 200

An out-of-scope question, a prompt-injection attempt, or a guard-tripped input all come back as `200` with
`refused: true` and the reason in `caveats` — they are not HTTP errors. The guard chain runs *inside*
`ask()`, same as the CLI: refusing is a normal, auditable outcome of asking a question, not a failure of the
transport. Only auth failures (401) and requests that never reach `ask()` (422 transport validation, 429
rate limit) are HTTP-level errors.

## One worker

`uvicorn --workers 1` is not a suggestion — `MODEL_RATE_LIMITER` (the fixed 20 rpm bucket in
`adpilot/core/models.py` that every model call blocks on) and `tools._EXEC_LOCK` (serializes DuckDB access)
are process-level state, each a single Python object living in one process's memory. A second worker process
gets its own copies of both, so it silently doubles the effective model rate and removes the DuckDB
serialization — `ADPILOT_API_RPM` cannot compensate, since it governs a different limiter (`app.state.limiter`,
one per process too). Scale by running more instances behind a load balancer, not more workers per instance
— see [deploy.md](deploy.md).
