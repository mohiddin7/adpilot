# Models

The agent and the eval judge each run on a **chain** of free OpenRouter models: a primary, then fallbacks in order,
with `openrouter/free` (which routes to whatever free model is up) as the last resort. Both chains use the same error
policy and the same code (`adpilot/core/models.py`). They never share a model.

## Chains

| | Default chain | Override |
|---|---|---|
| Agent | `inclusionai/ling-3.0-flash-fin:free` → `inclusionai/ling-3.0-flash-sante:free` → `nvidia/nemotron-3.5-lightning:free` → `openrouter/free` | `AGENT_LLM_TARGET_MODEL`, `AGENT_LLM_FALLBACK_MODEL` |
| Judge | `nex-agi/nex-n2.5-mini:free` → `dots-studio/dots-3-note-preview:free` → `cohere/north-mini-code:free` → `qwen/qwen3.8-27b:free` → `openrouter/free` | `JUDGE_LLM_TARGET_MODEL`, `JUDGE_LLM_FALLBACK_MODEL` |

`*_FALLBACK_MODEL` takes a comma-separated list. If it's unset, you get the default fallbacks. If it's empty (`""`),
there's no fallback. Duplicates are dropped and the order is kept.

The defaults come from a live probe on 2026-09-23. Every tool-capable free model was tried **alone** (no fallback)
through the real `ask()` (3 questions) and `judge_answer()` (3 calibration items):

| model (`:free`) | agent answered | agent avg | judge agree | judge avg | failure seen |
|---|---|---|---|---|---|
| inclusionai/ling-3.0-flash-fin | 3/3 | 13.6 s | 3/3 | 3.1 s | — |
| nex-agi/nex-n2.5-mini | 3/3 | 5.1 s | 3/3 | 2.5 s | — |
| nvidia/nemotron-3.5-lightning | 3/3 | 19.4 s | 3/3 | 45 s | — |
| inclusionai/ling-3.0-flash-sante | 2/3 | 4.5 s | 3/3 | 6.1 s | refused a valid question |
| openrouter/free | 2/3 | 5.2 s | 2/3 | 30 s | routed to sante / lfm; one tool loop |
| liquid/lfm-2.5-2.6b | 2/3 | 9.7 s | 2/3 | 3.5 s | refused a valid question |
| nex-agi/nex-n2.5-pro | 3/3 | 155 s | 3/3 | 115 s | far too slow |
| dots-studio/dots-3-note-preview | 1/3 | 6.1 s | 3/3 | 21.5 s | tool loop → 4-call cap |
| cohere/north-mini-code | 0/3 | — | 3/3 | 19.3 s | tool loop |
| qwen/qwen3.8-27b | 0/3 | — | 3/3 | 24 s | 429 upstream; 400 tool-schema rejected |
| nvidia/nemotron-3-super / -ultra / -nano | 0–1/3 | — | 1–3/3 | — | 503 overloaded, 502 ResourceExhausted |
| poolside/laguna-xs-2.1 / -s-2.1 | 0/3 | — | 2/3 | — | 429 upstream, tool loop |
| google/gemma-4-26b / -31b | 0/3 | — | 0–1/3 | — | 429 every call (shared AI Studio quota) |
| thinkingmachines/inkling / -small | 0/3 | — | 0/3 | — | 403 "only available on agentic harnesses" |

Three questions is a small sample. The chains are confirmed by the model-tier eval baseline, not by this table.

**The judge never grades its own model.** `judge_config()` refuses to start the eval (`ValueError`) if the two chains
share a model other than `openrouter/free`. `x/y` and `x/y:free` count as the same model. `openrouter/free` is allowed
in both chains, so a verdict it routes to one of the agent's models is thrown out as `judge_error: self-judged by …`.
It's excluded the same way a judge outage is.

## Error policy

`classify_error()` is the only place provider error codes are read. It sorts every error into one of three outcomes:

| condition | outcome | why |
|---|---|---|
| 429, `x-ratelimit-remaining: 0`, and the message says `per-day` (or, naming neither window, the reset is > 2 min away) | **stop** | the free daily cap is per account: every free model shares it |
| 401 | **stop** | bad or disabled key: no model will accept it |
| 402, except `metadata.limit_source == "openrouter_in_flight_budget"` | **stop** | no credits |
| 402 in-flight budget · 408 · any other 429 · 500 · 502 · 503 · 504 | **retry** | transient: timeout, per-model upstream limit, overload |
| 400 · 403 · 404 · 409 · 413 · 422 | **next** | this model can't serve this request (schema rejected, harness-only, withdrawn, too large) |
| no status (connection reset, DNS, read timeout) | **retry** | network |
| malformed response, anything else | **next** | fail over, never hang |

- **retry**: `Retrying` waits `min(retry-after, 10)` seconds, or 2 s then 5 s when there's no usable header, then tries
  the same model again. It retries at most twice, then falls over. Every attempt also waits its turn at the 20 rpm
  limiter. `Retrying` is the only retry layer: the OpenAI SDK's own retries are off (`max_retries = 0`), because they
  silently retried the daily-cap 429 for hours.
- **next**: move on to the next model in the chain.
- **stop**: the whole chain ends at once, and no later model is called. The question is then answered from the pack's
  canned queries. For the daily cap, the caveat says so: `daily free-model cap reached (resets 2026-09-24 00:00 UTC)`.

Free models are limited to 20 requests/minute and 50/day (1000/day with ≥ 10 credits purchased), **per account across
all keys**.
The per-minute window also answers 429 with `x-ratelimit-remaining: 0`, so that alone doesn't mean the daily cap:
a per-minute 429 is retried. The rate-limit headers are read from the response or, when OpenRouter only puts
them there, from the error body's `metadata.headers`.

## One re-run

When the agent's model fails on *quality* rather than availability, `ask()` gives the question one more chance on the
**next model in the chain**. Quality failures are:

- **a tool loop**: it used all 4 model calls without answering (`UsageLimitExceeded`);
- **invalid output**: its answer failed validation even after the agent's own retries (`UnexpectedModelBehavior`).

The re-run sends the user's question **unchanged**, followed by a note we write ourselves ("a previous attempt failed
(BudgetExceeded) … use at most two tool calls", or "(invalid output: …) return the final answer in the required
schema"), plus the last SQL error and its hint if there was one. That is exactly one re-run. If it fails too, the canned
query answers. There's no re-run when the failed model was the last one in the chain (including `openrouter/free`) or
when the chain has only one model. The worst case is 8 model calls per question.

The answer carries a `Rerun: <kind>` caveat, so re-run rates can be queried:

```sql
SELECT DATE(ts) AS day, COUNTIF(EXISTS(SELECT 1 FROM UNNEST(caveats) c WHERE STARTS_WITH(c, 'Rerun:'))) AS reruns, COUNT(*) AS asked
FROM `adpilot_audit.agent_calls` GROUP BY day ORDER BY day DESC
```

The audit still holds **one** record per question, with the user's own question. `requests` and tokens count both
attempts, and the `rerun` key of the `attributes` JSON says what the failed attempt did (model, calls, tools, SQL). `messages_json` holds only
the successful attempt, because sessions replay it as the next turn's history, and a dead tool loop must not end up
there.

## Tool schemas

Some providers enforce a strict grammar (qwen's, for one). They reject a *whole* request if any tool parameter is
nullable (`anyOf: [X, {"type": "null"}]`). The `NoNullSchemas` capability collapses those branches in the schemas
**the model is shown**, for the agent, the judge and the brief. Validation and the API's wire shape are unchanged:
a `null` in the model's output is still accepted, and `chart: null` is still returned.

## Re-probing a model

When a free slug disappears or a new one shows up, try it alone:

```bash
# agent candidate: one model, no fallback, nothing recorded
AGENT_LLM_TARGET_MODEL=vendor/model:free AGENT_LLM_FALLBACK_MODEL= ADPILOT_AUDIT=memory \
  adpilot chat -q "What was total spend per platform?"

# judge candidate: a few narrative cases plus the calibration check
JUDGE_LLM_TARGET_MODEL=vendor/model:free JUDGE_LLM_FALLBACK_MODEL= ADPILOT_AUDIT=memory \
  adpilot eval --tier model --family narrative --limit 3 --repeat 1 --out /tmp/probe
```

Then change the defaults in `adpilot/core/models.py` / `evals/judge.py` and this page, and re-baseline the model tier.

> ponytail: manual re-probe. Promote the throwaway probe script to `adpilot models probe` when a second free slug disappears.
