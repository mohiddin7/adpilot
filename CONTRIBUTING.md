# Contributing to AdPilot

Thanks for helping. The short version: keep `ruff` and `pytest` green, and say what you changed and why.

## Set up

```bash
git clone https://github.com/mohiddin7/adpilot.git && cd adpilot
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11, 3.12 or 3.13
pip install -e '.[dev]'
cp .env.example .env
```

No cloud account is needed: tests run on DuckDB over the bundled CSVs, with scripted models in place of real ones.

## Before you open a pull request

```bash
ruff check .
pytest -q
adpilot eval --check-cases     # the golden cases' reference SQL still runs
adpilot eval --tier deterministic --out .adpilot/evals   # no key needed
```

CI runs the same four steps on Python 3.11, 3.12 and 3.13.

## What goes where

- Domain-specific behaviour (tables, prompts, metric definitions, golden questions) belongs in a **pack**
  (`packs/ads/`), not in `adpilot/`. See "Point it at your own data" in the [README](README.md#-for-developers).
- A change to a guardrail needs a test that shows what it now refuses or allows, and an update to
  [docs/security.md](docs/security.md).
- A change to the prompt or the tools can move the eval score. The nightly model-tier run reports it; see
  [docs/evals.md](docs/evals.md).
- Every setting is read under one name. Add new variables to `.env.example` and the README's configuration table.

## Reporting bugs and ideas

Open an [issue](https://github.com/mohiddin7/adpilot/issues/new/choose). For security problems, follow
[SECURITY.md](SECURITY.md) instead.
