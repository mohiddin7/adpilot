"""Orchestrates one eval run: cases → task → evaluators → invariants → judge calibration → scorecard → reports."""

from __future__ import annotations

import itertools
import logging
import sys
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from adpilot.connectors import get_connector
from adpilot.core import schema
from adpilot.core.agent import build_agent
from adpilot.core.audit import MemorySink
from adpilot.core.models import DEFAULT_FALLBACK, DEFAULT_PRIMARY, build_model
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import REPO_ROOT, load_pack
from evals import deterministic
from evals import judge as judge_mod
from evals.cases import check_cases, load_cases, to_dataset
from evals.evaluators import Factual, Refuses, SafeSql, Trajectory
from evals.invariants import evaluate_invariants
from evals.judge import CalibratedJudge, build_judge_model, judge_config, run_calibration
from evals.scorecard import (
    Scorecard,
    build_scorecard,
    collect,
    gate,
    load_baseline,
    prompt_hash,
    update_badge,
    write_baseline,
    write_reports,
)
from evals.task import RecordingSource, load_fixtures, make_task

MODEL_REPORTS = REPO_ROOT / "evals" / "reports"
DET_REPORTS = REPO_ROOT / ".adpilot" / "evals"

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    scorecard: Scorecard | None
    ok: bool
    reasons: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    baseline: Scorecard | None = None


def run(
    *,
    tier: str = "deterministic",
    families: set[str] | None = None,
    repeat: int = 3,
    limit: int | None = None,
    out_dir: Path | None = None,
    check_only: bool = False,
    baseline_update: bool = False,
    no_judge: bool = False,
    pack_name: str = "ads",
    readme: Path = REPO_ROOT / "README.md",
    debug: bool = False,
) -> RunResult:
    if debug:
        # WARNING as the default keeps httpx/httpcore/asyncio's own (very verbose) debug logging
        # quiet; only our "evals" logger is turned up, so --debug shows just our per-case lines.
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        logging.getLogger("evals").setLevel(logging.DEBUG)
    pack = load_pack(pack_name)
    connector = get_connector("duckdb", pack)
    load_fixtures(connector, pack)
    cases = load_cases(pack, families, limit)
    problems = check_cases(connector, pack, cases)
    if check_only:
        return RunResult(None, not problems, problems=problems)
    if problems:
        return RunResult(None, False, problems=problems)

    out_dir = Path(out_dir) if out_dir else (MODEL_REPORTS if tier == "model" else DET_REPORTS)
    schema_text = schema.summary(connector, pack)

    def deps_factory() -> AgentDeps:
        return AgentDeps(connector=RecordingSource(connector), pack=pack, schema_text=schema_text, audit=MemorySink())

    judge_model = None
    models = {"agent_primary": None, "agent_fallback": None, "judge": None}
    if tier == "deterministic":
        if not limit:  # synthetic plumbing cases ride along unless the run was explicitly narrowed
            cases = cases + [c for c in deterministic.SYNTHETIC if not families or c.family in families]
        by_name = {c.name: c for c in cases}
        agent = build_agent()
        model_for = lambda inputs: deterministic.model_for(by_name[inputs.name], pack)  # noqa: E731
        models.update(agent_primary="scripted", agent_fallback="scripted")
    else:
        model = build_model()
        if model is None:
            return RunResult(None, False, problems=["no model API key configured (OPENROUTER_API_KEY or LLM_BEARER_TOKEN)"])
        agent = build_agent(model)
        model_for = None
        import os

        models.update(agent_primary=os.environ.get("LLM_TARGET_MODEL", DEFAULT_PRIMARY), agent_fallback=os.environ.get("LLM_FALLBACK_MODEL", DEFAULT_FALLBACK))
        if not no_judge:
            cfg = judge_config()
            judge_model = build_judge_model(cfg)
            models["judge"] = cfg.primary if judge_model else None

    judge_mod.CALLS["n"] = 0
    evaluators = [Factual(connector, pack), Refuses(), SafeSql(pack), Trajectory(), CalibratedJudge(judge_model, connector, pack)]
    task = make_task(agent, deps_factory, model_for)

    sample = [c for c in cases if c.consistency]
    total_tasks = len(cases) + (repeat * len(sample) if tier == "model" and repeat > 1 else 0)
    family_of = {c.name: c.family for c in cases}
    counter = itertools.count(1)
    # pydantic-evals' own bar and our own debug logging both want the terminal to themselves, so
    # debug mode gets a log line per case (with timing) instead of the bar.
    bar = (
        Progress(TextColumn("[progress.description]{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn())
        if sys.stdout.isatty() and not debug
        else None
    )

    def counted_task(inputs):
        i = next(counter)
        if debug:
            log.debug("[%d/%d] %s (%s): %s", i, total_tasks, inputs.name, family_of.get(inputs.name, "?"), inputs.final_question)
        trace = task(inputs)
        if debug:
            log.debug(
                "[%d/%d] %s done in %.2fs — %d model call(s), %d repair(s), refused=%s — %s",
                i, total_tasks, inputs.name, trace.duration_s, trace.model_calls, trace.repairs, trace.refused,
                (trace.answer.answer_md or "")[:160],
            )
        elif bar is not None:
            bar.update(bar_task_id, advance=1)
        return trace

    with bar or nullcontext():
        bar_task_id = bar.add_task("Evaluating", total=total_tasks) if bar else None
        report = to_dataset(cases, evaluators).evaluate_sync(counted_task, max_concurrency=1, progress=False)

        repeat_report = None
        if tier == "model" and repeat > 1 and sample:
            repeat_report = to_dataset(sample, [Factual(connector, pack)]).evaluate_sync(
                counted_task, max_concurrency=1, progress=False, repeat=repeat
            )

    traces = {c.name: c.output for c in report.cases if c.output is not None}
    invariants = evaluate_invariants(traces, connector, pack)
    calibration = run_calibration(judge_model, pack) if judge_model else None
    judge_reliable = bool(calibration and calibration.reliable)
    results = collect(report, judge_reliable)
    repeats = collect(repeat_report, judge_reliable) if repeat_report else []
    calls = sum(t.model_calls for t in traces.values()) + sum(getattr(c.output, "model_calls", 0) for c in (repeat_report.cases if repeat_report else [])) + judge_mod.CALLS["n"]

    baseline = load_baseline(out_dir / "baseline.json")
    sc = build_scorecard(results, repeats, invariants, calibration, tier=tier, prompt_hash=prompt_hash(pack), models=models, calls_used=calls, baseline=baseline)
    if baseline_update:
        write_baseline(sc, out_dir / "baseline.json")
        baseline = sc
    write_reports(sc, out_dir, baseline)
    if tier == "model" and Path(out_dir).resolve() == MODEL_REPORTS.resolve() and Path(readme).exists():
        update_badge(readme, sc.overall)
    ok, reasons = gate(sc, baseline)
    return RunResult(sc, ok, reasons=reasons, baseline=baseline)
