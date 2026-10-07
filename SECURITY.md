# Security policy

AdPilot runs model-written SQL against your data, so its guardrails are a core feature. How each layer works and
what it cannot catch is in [docs/security.md](docs/security.md).

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private reporting instead:
[Report a vulnerability](https://github.com/mohiddin7/adpilot/security/advisories/new).

Useful things to include: the version or commit, the question or request that triggers it, and what it exposes
(data read, a write that got through, a secret in an answer, a guard that was skipped).

## Scope

In scope: the input guard, the SQL guard, output redaction, API authentication and rate limiting, the MCP server,
and anything that lets a question read or change data it should not.

Out of scope: the free models' answer quality (see [docs/evals.md](docs/evals.md)), and findings that need an
attacker who already holds `ADPILOT_API_KEY` or your cloud credentials.

## Supported versions

Only the latest commit on `main` is supported.
