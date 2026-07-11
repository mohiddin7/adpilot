"""adpilot CLI: `adpilot chat [-q QUESTION]` and `adpilot schema`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from adpilot.connectors import get_connector
from adpilot.core import schema
from adpilot.core.agent import AnalystAnswer, ask, build_agent
from adpilot.core.memory import SessionStore
from adpilot.core.models import build_model
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import load_pack

SESSIONS_DB = Path(".adpilot") / "sessions.db"


def _deps(args) -> AgentDeps:
    pack = load_pack(args.pack)
    name = args.connector or os.environ.get("ADPILOT_CONNECTOR") or pack.raw["connector"]
    connector = get_connector(name, pack)
    return AgentDeps(connector=connector, pack=pack, schema_text=schema.summary(connector, pack))


def _print(answer: AnalystAnswer, out) -> None:
    print(answer.answer_md, file=out)
    if answer.sql:
        print(f"\nSQL:\n{answer.sql}", file=out)
    if answer.data:
        print("\n" + pd.DataFrame(answer.data).to_string(index=False), file=out)
    if answer.chart:
        print(f"\nChart: {answer.chart.chart_type} x={answer.chart.x} y={answer.chart.y}", file=out)
    for c in answer.caveats:
        print(f"\n⚠ {c}", file=out)
    print(f"\n(confidence {answer.confidence:.1f})", file=out)


def cmd_schema(args, out) -> int:
    print(_deps(args).schema_text, file=out)
    return 0


def cmd_eval(args, out) -> int:
    from evals.run import run

    res = run(
        tier=args.tier, families=set(args.family) if args.family else None, repeat=args.repeat, limit=args.limit,
        out_dir=Path(args.out) if args.out else None, check_only=args.check_cases, baseline_update=args.baseline_update, no_judge=args.no_judge,
        pack_name=args.pack, debug=args.debug,
    )
    for p in res.problems:
        print(f"problem: {p}", file=out)
    if res.scorecard is None:
        print("cases OK" if res.ok else "harness error", file=out)
        return 0 if res.ok else (1 if args.check_cases else 2)
    from evals.scorecard import render_markdown

    print(render_markdown(res.scorecard, res.baseline), file=out)
    for r in res.reasons:
        print(f"GATE: {r}", file=out)
    return 0 if res.ok else 1


def cmd_chat(args, out) -> int:
    deps = _deps(args)
    model = build_model()
    agent = build_agent(model)
    if model is None:
        print("No OPENROUTER_API_KEY set — answering from pre-defined queries only.\n", file=out)
    store = SessionStore(SESSIONS_DB) if args.session else None

    def one(question: str) -> None:
        history = store.load(args.session) if store else None
        answer, new_messages = ask(agent, deps, question, history=history)
        if store and new_messages:
            store.save(args.session, new_messages)
        _print(answer, out)

    if args.question:
        one(args.question)
        return 0
    print(f"AdPilot · {deps.connector.dialect} · pack={deps.pack.name}. Ctrl-D to quit.", file=out)
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            return 0
        if q:
            one(q)


def main(argv: list[str] | None = None, out=None) -> int:
    load_dotenv()
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="adpilot", description="Agentic analytics over your data.")
    p.add_argument("--pack", default="ads", help="pack name or path (default: ads)")
    p.add_argument("--connector", choices=["duckdb", "bigquery"], help="overrides ADPILOT_CONNECTOR / pack default")
    sub = p.add_subparsers(dest="cmd", required=True)
    chat = sub.add_parser("chat", help="ask questions (REPL without -q)")
    chat.add_argument("-q", "--question")
    chat.add_argument("--session", help="session id; keeps the last 3 turns as context")
    sub.add_parser("schema", help="print the tables the agent can query")
    ev = sub.add_parser("eval", help="run the eval harness")
    ev.add_argument("--tier", choices=["deterministic", "model"], default="deterministic")
    ev.add_argument("--family", action="append", help="restrict to a family (repeatable)")
    ev.add_argument("--repeat", type=int, default=3, help="runs per consistency case (model tier)")
    ev.add_argument("--limit", type=int)
    ev.add_argument("--check-cases", action="store_true", help="only verify reference values; no model")
    ev.add_argument("--out", help="report directory (default: evals/reports for model tier)")
    ev.add_argument("--baseline-update", action="store_true", help="write baseline.json from this run")
    ev.add_argument("--no-judge", action="store_true")
    ev.add_argument("--debug", action="store_true", help="log each case as it runs; disables the progress bar")
    args = p.parse_args(argv)
    return {"chat": cmd_chat, "schema": cmd_schema, "eval": cmd_eval}[args.cmd](args, out)


if __name__ == "__main__":
    sys.exit(main())
