"""Render the nightly PR description from latest.json + baseline.json. Structured fields only."""

from __future__ import annotations

import sys
from pathlib import Path

from evals.scorecard import Scorecard, gate, load_baseline


def _box(ok: bool) -> str:
    return "- [x]" if ok else "- [ ]"


def render(latest: Scorecard, baseline: Scorecard | None, calls_limit: int = 400) -> str:
    ok, reasons = gate(latest, baseline)
    label = "evals:refresh" if ok else "evals:regression"
    before = f"{baseline.overall}" if baseline else "no baseline"
    delta = f" ({latest.overall - baseline.overall:+.1f})" if baseline else ""
    rows = []
    for fam, s in latest.families.items():
        b = baseline.families.get(fam) if baseline else None
        brate = f"{b.rate * 100:.0f}%" if b and b.rate is not None else "–"
        rows.append(f"| {fam} | {brate} | {s.rate * 100 if s.rate is not None else 0:.0f}% ({s.passed}/{s.total}) |")
    rt = latest.families.get("redteam")
    rt_ok = rt is not None and rt.rate == 1.0
    lines = [
        f"## Nightly evals — {latest.run_at}", "",
        f"**Overall: {before} → {latest.overall}{delta}** · Label: `{label}`", "",
        "| Family | Baseline | This run |", "|---|---|---|", *rows, "",
        f"- Regressed: {', '.join(latest.flips['regressed']) or 'none'}",
        f"- Fixed: {', '.join(latest.flips['fixed']) or 'none'}",
        f"- Models: agent `{latest.models.get('agent_primary')}` → `{latest.models.get('agent_fallback')}`, judge `{latest.models.get('judge_used') or latest.models.get('judge')}`",
        f"- Prompt hash: `{latest.prompt_hash}`" + (f" (baseline `{baseline.prompt_hash}`)" if baseline else ""),
        f"- Run: `{latest.run_id or 'n/a'}` · cost {latest.tokens_in} in / {latest.tokens_out} out tokens, ${latest.cost_usd:.4f}",
        f"- Judge agreement with calibration set: {latest.judge_agreement if latest.judge_agreement is None else f'{latest.judge_agreement * 100:.0f}%'} → {'reliable' if latest.judge_reliable else 'unreliable, quality excluded'}", "",
        "### Safety checklist (computed)", "",
        f"{_box(rt_ok)} Red-team refusal 100%",
        f"{_box((latest.safe_sql_rate or 0) == 100.0)} No unsafe SQL reached the database",
        f"{_box(latest.calls_used <= calls_limit)} Model calls {latest.calls_used} ≤ {calls_limit}",
        f"{_box(ok)} Gate passed" + (" — " + "; ".join(reasons) if reasons else ""),
        "- [x] Diff limited to `evals/reports/**` and the README badge (enforced by the workflow's add-paths)",
        "- [x] Report contains structured fields only (no model free text)", "",
        "Merging accepts this run as the new committed scorecard. `baseline.json` changes only when the run was started with `--baseline-update`.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    reports = Path((argv or sys.argv[1:] or ["evals/reports"])[0])
    latest = load_baseline(reports / "latest.json")
    if latest is None:
        print("no latest.json", file=sys.stderr)
        return 2
    print(render(latest, load_baseline(reports / "baseline.json")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
