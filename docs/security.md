# Security: defense in depth

No single control is "the fix". Each layer below exists because of what the layers above and below it
cannot catch, and the eval asserts on the *system* refusing, never on which layer did it.

| # | Layer | Where | `error_kind` when it refuses |
|---|---|---|---|
| 0 | Deterministic input gate | `sanitize_question` in `adpilot/core/guardrails.py` | `InputPolicy` |
| 1 | Semantic input classifier | `classify_question` in `adpilot/core/guardrails.py` — Jev Choice behind `ADPILOT_INPUT_CLASSIFIER=jev`, off by default | `InputPolicy` + caveat `classifier:jev` |
| 2 | LLM + system prompt | `packs/ads/prompts/` | `OutOfScope` (model `Refusal`) |
| 3 | Execution validator | `validate_sql` — SELECT-only, one statement, row cap; every FROM/JOIN item (comma lists, subqueries, CTEs) must be an allowlisted table, and a string, table function or LATERAL there is refused | `SqlPolicy` |
| 4 | Least-privilege credentials | service account is `READER` on data, `WRITER` on the audit dataset only ([observability.md](observability.md)). DuckDB: data is loaded at connect, then `enable_external_access = false` and `lock_configuration = true`, so no query reads a file | — |
| 5 | Output rail | `redact_output` — masks PII / secret shapes in `answer_md` | `OutputPolicy` |

Layers 3 and 4 are the wall: even if every text layer fails, the database receives one read-only SELECT
against allowlisted tables under an account that cannot write them. Layers 0 and 1 keep the red-team rate at
100% under paraphrase; layer 5 stops a leak that slipped through everything above.

## Layer 0 — what the input gate refuses

In order, after NFKC normalisation and stripping zero-width / bidi / BOM characters:

1. a token that mixes Latin with Cyrillic or Greek letters (homoglyph attacks; fail closed);
2. instruction-override phrases, `system:` / `<system` tags, act-as-admin, jailbreak / DAN / developer mode;
3. the SQL validator's own metadata and file-read pattern (`information_schema`, `pg_catalog`,
   `sqlite_master`, `__tables__`, `@@version`, `duckdb_*(`, `read_*(`) applied to the question;
4. SQL write statements in natural language — `drop/truncate/alter table`, `delete from`, `insert into`,
   `update … set`, "update the *x* column to" — bare or smuggled behind `;`, `--` or `/*`;
5. a SCREAMING_SNAKE credential name (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`).

Plain English that uses the same words passes: "which campaigns should we drop", "did spend update after
the sync", "what tables do you use", accented names, currency symbols, a bare table name.

Known ceilings: bare `; select …` stacking is left to layer 3 (it collides with English); rewordings that carry
no SQL or override token at all are layer 1's job.

## Layer 5 — the output rail

`redact_output` replaces emails, phone numbers (international `+…` and US shapes only, so dates, currency
and row counts never match) and key-shaped tokens (`sk-…`, `AIza…`, `Bearer …`, `NAME_KEY=value`) with
`[redacted:<kind>]`, appends the `OutputPolicy` caveat, and lets the rest of the answer through. It runs on
every exit path of `ask()`. Result rows are not scanned: they come from allowlisted tables with no PII columns.

## Measuring it

Every refusal is a row in `agent_calls` with `error_kind` naming the layer, so trip rates per layer are one
query away:

```sql
SELECT error_kind, COUNT(*) AS n
FROM `adpilot_audit.agent_calls`
WHERE refused AND ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY 1 ORDER BY n DESC
```

Layer 0 and layer 1 share `error_kind = InputPolicy`; the caveat tells them apart, and degraded calls (layer 1
unreachable, answer served on layer 0 alone) are their own row:

```sql
SELECT
  CASE WHEN 'classifier:jev' IN UNNEST(caveats) THEN 'layer 1'
       WHEN 'GuardDegraded' IN UNNEST(caveats) THEN 'layer 1 degraded'
       ELSE 'layer 0' END AS layer, COUNT(*) AS n
FROM `adpilot_audit.agent_calls`
WHERE (error_kind = 'InputPolicy' OR 'GuardDegraded' IN UNNEST(caveats))
  AND ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY 1 ORDER BY n DESC
```

The eval gate ([evals.md](evals.md)) enforces two numbers: red-team refusals **100%** and guard
false-positive rate **0%** on the eval corpus. Red-team cases marked `guard: true` are run in the
deterministic tier against a *compliant* scripted model, so they only pass when layer 0 refused before the
model ran. Every non-refusing case is a negative test for the guard.

## Adding a guard pattern

A pattern ships with both halves or not at all: a `MUST_BLOCK` entry (plus its casing / spacing /
character-injection / reworded variants) and a `MUST_PASS` entry that uses the same words legitimately, in
`tests/test_guardrails.py`; and matching `guard: true` red-team and `sc_on_topic_*` cases in
`packs/ads/evals.yaml`. If the false-positive rate moves above 0, the pattern is wrong, not the case.

## Layer 1 — semantic classifier

`classify_question` runs after every layer-0 check passed and asks one Choice question of
[Jev](https://docs.typesafe.ai) (TypeSafe AI): is this message `safe`, an `injection`, or `out_of_scope`?
Jev is not an LLM — it returns calibrated probabilities and cannot generate text, so it cannot be talked into
answering. It is the only backend; the interface is one env var and one function, so a second one is a small change.

- **Off by default.** `ADPILOT_INPUT_CLASSIFIER` unset → the function returns immediately, no network.
  `=jev` needs `JEV_API_KEY`. Any other value logs a warning and behaves as unset.
- **Refuse** when `P(injection) ≥ 0.7`, or `P(out_of_scope) ≥ 0.7`, or `P(safe) < 0.5` — safe must hold the
  majority. (The original design refused on low *confidence*; on the measured corpus that clause produced
  every false positive — all safe-vs-off-topic ties with `P(injection) = 0` — and caught nothing the other
  clauses missed, so it was replaced by the `P(safe)` clause, which keeps the fail-closed property on the axis
  that matters.)
- **Degradation:** unreachable, non-2xx, malformed, missing key, or slower than **2 s** → fall back to layer 0
  only, `log.warning("input classifier degraded: …")`, caveat `GuardDegraded` on the answer. An outage of an
  optional layer must not be a product outage, because layers 3 + 4 are the wall.
- **Measured (2026-09-21, 143 questions):** 99 legitimate questions incl. novice definitions and six
  adversarial negatives ("Ignore TikTok and compare…", "Drop the campaigns with zero conversions…"), 44 attacks
  incl. **12 rewordings that carry no SQL keyword or override token**. Layer 0 caught 0/12 of those; Jev caught
  44/44 with **0 false positives** (budget ≤ 2%), 0.32 s mean / 0.54 s max latency, ~$0.00002 per call. Widest
  legitimate `P(injection)` was 0.23; the least suspicious attack scored 0.80.
- **Evals:** the 12 rewordings are red-team cases marked `classifier: true`. They run only in the model tier
  with the flag set (the nightly); otherwise they are skipped with a warning, never counted as passed, so a run
  without layer 1 cannot prove — or fake — layer 1. Tests fake the backend; nothing in the suite touches the network.
- **Not the judge.** Jev Score was also measured as the eval judge against the 52-entry human-labelled
  calibration set: 47/52 vs 49/52 for the best free LLM judge under the same rubric. It did not clear the bar
  and was not wired in.
