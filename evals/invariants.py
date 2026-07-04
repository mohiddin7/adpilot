"""Metamorphic checks across cases: the agent's own answers must agree with each other."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from evals.task import Trace

TOL = 0.01


@dataclass
class InvariantResult:
    name: str
    passed: bool | None
    reason: str


@dataclass
class Rule:
    name: str
    needs: tuple[str, ...]
    check: Callable[[dict[str, list[dict]], Any, Any], tuple[bool, str]]


def _num(row: dict, *keys: str) -> float | None:
    for k in keys:
        if k in row and isinstance(row[k], int | float):
            return float(row[k])
    vals = [v for v in row.values() if isinstance(v, int | float) and not isinstance(v, bool)]
    return float(vals[0]) if vals else None


def _str(row: dict, *keys: str) -> str | None:
    for k in keys:
        if k in row and isinstance(row[k], str):
            return row[k]
    return next((v for v in row.values() if isinstance(v, str)), None)


def _sum_matches(parts: list[dict], total: list[dict], col: str) -> tuple[bool, str]:
    s = sum(_num(r, col) or 0.0 for r in parts)
    t = _num(total[0], col) if total else None
    if t is None:
        return False, "total row has no number"
    return abs(s - t) <= TOL * max(abs(t), 1e-9), f"parts sum {s:.2f} vs total {t:.2f}"


def _spend_sum(d, con, pack):
    return _sum_matches(d["spend_by_platform"], d["total_spend"], "spend")


def _conv_sum(d, con, pack):
    return _sum_matches(d["conversions_by_platform"], d["total_conversions"], "conversions")


def _worst_in_ranking(d, con, pack):
    worst = _str(d["worst_cpa_campaign"][0], "campaign_name")
    names = {_str(r, "campaign_name") for r in d["cpa_ranking"][:10]}
    return worst in names, f"{worst} in top-10 ranking: {worst in names}"


def _top_platform(d, con, pack):
    top_platform = max(d["spend_by_platform"], key=lambda r: _num(r, "spend") or 0)
    camp = _str(d["top_spend_campaign"][0], "campaign_name")
    rows = con.query(pack.render("SELECT platform FROM {gold} WHERE campaign_name = ? LIMIT 1".replace("?", f"'{camp}'"), con.dialect))
    plat = rows["platform"][0] if len(rows) else None
    return plat == _str(top_platform, "platform"), f"top campaign {camp} is on {plat}; top platform {_str(top_platform, 'platform')}"


def _anomaly_platforms(d, con, pack):
    known = {_str(r, "platform") for r in d["spend_by_platform"]}
    seen = {_str(r, "platform") for r in d["anomalies_list"]}
    return seen <= known, f"anomaly platforms {sorted(seen)} within {sorted(known)}"


def _budget_balance(d, con, pack):
    cur = sum(_num(r, "current_spend") or 0 for r in d["budget_plan"])
    rec = sum(_num(r, "recommended_spend") or 0 for r in d["budget_plan"])
    return abs(cur - rec) <= TOL * max(cur, 1e-9), f"current {cur:.2f} vs recommended {rec:.2f}"


RULES: list[Rule] = [
    Rule("platform_spend_sums_to_total", ("spend_by_platform", "total_spend"), _spend_sum),
    Rule("platform_conversions_sum_to_total", ("conversions_by_platform", "total_conversions"), _conv_sum),
    Rule("worst_cpa_in_ranking", ("worst_cpa_campaign", "cpa_ranking"), _worst_in_ranking),
    Rule("top_spend_platform_consistent", ("spend_by_platform", "top_spend_campaign"), _top_platform),
    Rule("anomaly_platforms_exist", ("spend_by_platform", "anomalies_list"), _anomaly_platforms),
    Rule("budget_totals_balance", ("budget_plan",), _budget_balance),
]


def evaluate_invariants(traces: dict[str, Trace], connector: Any, pack: Any) -> list[InvariantResult]:
    out: list[InvariantResult] = []
    for rule in RULES:
        data = {n: (traces[n].answer.data or []) for n in rule.needs if n in traces}
        if len(data) < len(rule.needs) or any(not v for v in data.values()):
            out.append(InvariantResult(rule.name, None, "skipped: missing case or no data"))
            continue
        try:
            ok, why = rule.check(data, connector, pack)
        except Exception as exc:  # noqa: BLE001 — a rule that cannot evaluate is a failure, not a crash
            ok, why = False, f"error: {exc}"[:200]
        out.append(InvariantResult(rule.name, ok, why))
    return out
