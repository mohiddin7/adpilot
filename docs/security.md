# Security: defense in depth

No single control is "the fix". Each layer below exists because of what the layers above and below it
cannot catch, and the eval asserts on the *system* refusing, never on which layer did it.

| # | Layer | Where | `error_kind` when it refuses |
|---|---|---|---|
| 0 | Deterministic input gate | `sanitize_question` in `adpilot/core/guardrails.py` | `InputPolicy` |
| 1 | Semantic input classifier | not yet built — see below | `InputPolicy` |
| 2 | LLM + system prompt | `packs/ads/prompts/` | `OutOfScope` (model `Refusal`) |
| 3 | Execution validator | `validate_sql` — SELECT-only, table allowlist, one statement, row cap | `SqlPolicy` |
| 4 | Least-privilege credentials | service account is `READER` on data, `WRITER` on the audit dataset only ([observability.md](observability.md)) | — |
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

The eval gate ([evals.md](evals.md)) enforces two numbers: red-team refusals **100%** and guard
false-positive rate **0%** on the eval corpus. Red-team cases marked `guard: true` are run in the
deterministic tier against a *compliant* scripted model, so they only pass when layer 0 refused before the
model ran. Every non-refusing case is a negative test for the guard.

## Adding a guard pattern

A pattern ships with both halves or not at all: a `MUST_BLOCK` entry (plus its casing / spacing /
character-injection / reworded variants) and a `MUST_PASS` entry that uses the same words legitimately, in
`tests/test_guardrails.py`; and matching `guard: true` red-team and `sc_on_topic_*` cases in
`packs/ads/evals.yaml`. If the false-positive rate moves above 0, the pattern is wrong, not the case.

## Layer 1 — semantic classifier (designed, not built)

A classifier that returns `{safe, injection, out_of_scope}` with a confidence, called after layer 0 passes.
Decisions already made so the implementation is not left to guess:

- **Refuse** when `P(injection)` or `P(out_of_scope)` ≥ 0.7, **and** when the top class's confidence < 0.5 —
  unknown is unsafe.
- **Degradation:** backend unreachable, non-2xx or slower than 2 s → fall back to layer 0 only, log a
  warning, append the `GuardDegraded` caveat. An outage of an optional layer must not be a product outage,
  because layers 3 + 4 are the wall; the degraded call stays queryable and alertable.
- **Off by default.** `ADPILOT_INPUT_CLASSIFIER` unset → skipped entirely, no network. Backends (Jev Choice,
  Llama Prompt Guard 2, Lakera) are optional dependencies, never hard ones, and adoption is gated on
  agreement with the calibration set, not on a vendor claim.
- **False-positive budget ≤ 2%** on the eval corpus (probabilistic layer); layer 0 keeps 0%.
- Refusals record `InputPolicy` with the caveat `classifier:<backend>`, so layer 0 and layer 1 trips stay
  separable without a schema change. Tests fake the backend.
