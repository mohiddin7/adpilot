"""adpilot CLI: `adpilot chat [-q QUESTION]`, `adpilot schema`, `adpilot brief`, `adpilot eval` and `adpilot audit`."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from adpilot.core.agent import AnalystAnswer, ask, build_agent
from adpilot.core.audit import (
    AuditSink,
    MemorySink,
    RunContextInfo,
    audit_config,
    build_sink,
)
from adpilot.core.models import build_model
from adpilot.core.runtime import announce_audit, build_deps, configure_tracing, open_sink
from adpilot.core.tools import AgentDeps


def _deps(args, audit: AuditSink) -> AgentDeps:
    return build_deps(args.pack, args.connector, audit)


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
    print(_deps(args, MemorySink()).schema_text, file=out)
    return 0


def cmd_eval(args, out) -> int:
    from evals.run import run

    if not args.check_cases:
        # The deterministic tier never leaves memory regardless of ADPILOT_AUDIT (evals/run.py always
        # gives it a MemorySink); the model tier honours the real config, same as chat/audit.
        cfg = audit_config()
        mode = "memory" if args.tier == "deterministic" else cfg.mode
        announce_audit(mode, cfg.project, cfg.dataset, out)

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
    if res.audit is not None and not res.audit.ok:
        if res.run_id:
            print(f"run_id: {res.run_id}", file=out)
        print(f"audit: {res.audit.pending} row(s) not persisted — " + "; ".join(res.audit.errors), file=out)
        return 2
    if res.run_id:
        print(f"run_id: {res.run_id}", file=out)
    return 0 if res.ok else 1


def cmd_chat(args, out) -> int:
    sink = open_sink(out)
    if sink is None:
        return 2
    deps = _deps(args, sink)
    deps.run_context = RunContextInfo(source="chat", session_id=args.session)
    model = build_model()
    agent = build_agent(model)
    if model is None:
        print("No AGENT_LLM_BEARER_TOKEN set — answering from pre-defined queries only.\n", file=out)
    history = sink.load_session(args.session) if args.session else []
    unflushed = 0

    def one(question: str) -> None:
        nonlocal unflushed
        answer, new_messages, _ = ask(agent, deps, question, history=history or None)
        history.extend(new_messages)
        try:
            _print(answer, out)
        finally:
            # ask() already buffered this turn's record; flush it even if printing the answer raised
            # (e.g. a malformed answer.data DataFrame), so the record is never lost with the REPL.
            rep = sink.flush()
            unflushed = rep.pending
            if not rep.ok:
                print(f"\n⚠ audit: {rep.pending} row(s) not persisted (will retry with the next turn): {'; '.join(rep.errors)}", file=out)

    if args.question:
        one(args.question)
        return 1 if unflushed else 0
    print(f"AdPilot · {deps.connector.dialect} · pack={deps.pack.name}. Ctrl-D to quit.", file=out)
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            rep = sink.flush()
            if not rep.ok:
                print(f"audit: {rep.pending} row(s) not persisted", file=out)
                return 1
            return 0
        if q:
            one(q)


def cmd_brief(args, out) -> int:
    """Markdown on `out` (or --out); every status line on stderr so `adpilot brief > brief.md` stays clean."""
    from adpilot.core.brief import questions, run_brief

    err = sys.stderr
    sink = open_sink(err)
    if sink is None:
        return 2
    deps = _deps(args, sink)
    if not questions(deps.pack):
        print(f"pack {deps.pack.name} defines no briefing questions (pack.yaml: briefing.questions)", file=err)
        return 2
    model = build_model()
    if model is None:
        print("No AGENT_LLM_BEARER_TOKEN set — every question falls back and there is no synthesis.", file=err)
    brief = run_brief(deps, build_agent(model), model)
    if args.out:
        Path(args.out).write_text(brief.markdown)
    else:
        print(brief.markdown, file=out)
    rep = sink.flush()
    if not rep.ok:
        print(f"audit: {rep.pending} row(s) not persisted — {'; '.join(rep.errors)}", file=err)
        return 2
    return 1 if brief.unavailable or brief.summary is None else 0


def cmd_audit(args, out) -> int:
    if args.audit_cmd == "preflight":
        return 0 if open_sink(out) is not None else 2
    cfg = audit_config()
    sink = build_sink(cfg)
    if cfg.mode == "memory" and args.audit_cmd != "export":
        # export's stdout is machine-parseable (JSONL/CSV); a banner line would corrupt it.
        print("audit: memory — nothing is stored in this mode", file=out)
    if args.audit_cmd == "runs":
        rows = sink.list_runs(args.limit)
        if not rows:
            print("no runs", file=out)
            return 0
        print(f"{'run_id':<32} {'ts':<26} {'tier':<13} {'overall':>7} {'gate':<5} {'calls':>5} {'cost_usd':>9}", file=out)
        for r in rows:
            print(f"{r['run_id']:<32} {str(r['ts'])[:26]:<26} {r.get('tier') or '':<13} {float(r.get('overall') or 0):>7.1f} {'PASS' if r.get('gate_ok') else 'FAIL':<5} {int(r.get('calls_used') or 0):>5} {float(r.get('cost_usd') or 0):>9.4f}", file=out)
        return 0
    rows = list(sink.export_run(args.run))
    if not rows:
        print(f"no rows for run {args.run}", file=out)
        return 1
    if args.csv:
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow({k: (json.dumps(v, default=str) if isinstance(v, list | dict) else v) for k, v in r.items()})
        return 0
    for r in rows:
        print(json.dumps(r, default=str), file=out)
    return 0


def main(argv: list[str] | None = None, out=None) -> int:
    load_dotenv()
    configure_tracing()
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="adpilot", description="Agentic analytics over your data.")
    p.add_argument("--pack", default="ads", help="pack name or path (default: ads)")
    p.add_argument("--connector", choices=["duckdb", "bigquery"], help="overrides ADPILOT_CONNECTOR / pack default")
    sub = p.add_subparsers(dest="cmd", required=True)
    chat = sub.add_parser("chat", help="ask questions (REPL without -q)")
    chat.add_argument("-q", "--question")
    chat.add_argument("--session", help="session id; keeps the last 3 turns as context")
    sub.add_parser("schema", help="print the tables the agent can query")
    br = sub.add_parser("brief", help="write the daily brief as markdown")
    br.add_argument("--out", help="write the brief to FILE instead of stdout")
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
    au = sub.add_parser("audit", help="inspect the audit trail")
    aus = au.add_subparsers(dest="audit_cmd", required=True)
    aus.add_parser("preflight", help="verify credentials, dataset and tables (creates/alters as needed)")
    runs = aus.add_parser("runs", help="recent eval runs")
    runs.add_argument("--limit", type=int, default=20)
    ex = aus.add_parser("export", help="one run's calls with their scores, as JSONL (or --csv)")
    ex.add_argument("--run", required=True)
    ex.add_argument("--csv", action="store_true")
    args = p.parse_args(argv)
    commands = {"chat": cmd_chat, "schema": cmd_schema, "brief": cmd_brief, "eval": cmd_eval, "audit": cmd_audit}
    return commands[args.cmd](args, out)


if __name__ == "__main__":
    sys.exit(main())
