"""The daily brief: decisions first. Code reads the data and computes every fact (analyses.py), one tool-less model
call words them (writer.py), and render.py lays them out for a public GitHub issue.

Nothing here goes through the agent: the brief runs four fixed read-only queries, so its numbers never depend on
model-written SQL. Items it publishes are stored in the audit sink's `brief_items` table, and the next brief
follows up on them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pandas as pd
from pydantic_ai.models import Model

from adpilot.brief import analyses as a
from adpilot.brief.render import render
from adpilot.brief.writer import write
from adpilot.core.audit import BriefItemRow, new_trace_id
from adpilot.core.guardrails import redact_output
from adpilot.core.tools import AgentDeps
from adpilot.packs.loader import Pack

DEFAULTS = {
    "mature_lag_days": 2,  # the newest days still gain late conversions (pipelines/common.py RESTATING_DAYS)
    "cost_rise_pct": 15,
    "min_conversions": 30,
    "anomaly_persist_usd": 100,
    "anomaly_any_usd": 500,
    "tracking_min_daily_sales": 5,
    "tracking_drop_ratio": 0.5,
    "double_count_ratio": 3.0,
    "clicks_normal_band": 0.3,
    "pacing_on_track_pct": 5,
    "pacing_item_pct": 10,
    "outlook_worse_pct": 10,
    "outlook_uncertain_ratio": 0.5,
    "move_min_share_pct": 10,
    "move_slice_pct": 20,
    "move_stop_ratio": 1.15,
    "max_items": 3,
    "followup_days": 14,
}


def settings(pack: Pack) -> tuple[dict, dict]:
    """(thresholds, monthly budgets) from pack.yaml `briefing`. A typo or a non-positive value is an error: a silently
    ignored threshold would change what the brief tells people."""
    cfg = pack.raw.get("briefing") or {}
    unknown = set(cfg) - {"thresholds", "budgets"} | set(cfg.get("thresholds") or {}) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"pack.yaml briefing: unknown key(s) {sorted(unknown)}")
    t = {**DEFAULTS, **(cfg.get("thresholds") or {})}
    budgets = cfg.get("budgets") or {}
    bad = [k for k, v in {**t, **budgets}.items() if not isinstance(v, (int, float)) or v < 0 or (v == 0 and k != "mature_lag_days")]
    if bad:
        raise ValueError(f"pack.yaml briefing: {sorted(bad)} must be positive numbers")
    return t, budgets


@dataclass
class Brief:
    run_id: str
    as_of: date | None
    items: list[a.Item]
    problems: list[str]  # data or memory the brief could not read: the job fails, the brief still publishes
    caveats: list[str]
    markdown: str
    rows: list[BriefItemRow] = field(default_factory=list)


def _dates(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    for c in cols:
        df[c] = pd.to_datetime(df[c]).dt.date
    return df


def load(deps: AgentDeps, t: dict) -> tuple[dict, list[str]]:
    """The four read-only queries. A failed one leaves its frame out and says so; gold failing ends the brief."""
    con, pack = deps.connector, deps.pack

    def q(sql: str) -> pd.DataFrame:
        return con.query(pack.render(sql, con.dialect), max_bytes=pack.raw.get("max_bytes_billed"))

    data, problems = {}, []
    latest = q("SELECT MAX(date) AS d FROM {gold}")["d"].iloc[0]
    if latest is None or pd.isna(latest):
        return data, ["the ad data is empty"]
    latest = pd.to_datetime(latest).date()
    data["latest"], data["as_of"] = latest, latest - timedelta(days=t["mature_lag_days"])
    since = latest - timedelta(days=45)  # covers month-to-date, two weeks, and the 14-day baselines before them
    data["gold"] = _dates(q(
        "SELECT date, platform, campaign_id, campaign_name, SUM(impressions) AS impressions, SUM(clicks) AS clicks, "
        f"SUM(spend) AS spend, SUM(conversions) AS conversions FROM {{gold}} WHERE date >= DATE '{since}' "
        "GROUP BY date, platform, campaign_id, campaign_name"), "date")
    week_start = data["as_of"] - timedelta(days=6)
    for name, sql, cols in (
        ("flags", f"SELECT * FROM {{anomalies}} WHERE is_anomaly = 1 AND date >= DATE '{week_start}'", ("date",)),
        ("plan", "SELECT * FROM {budget} WHERE generated_at = (SELECT MAX(generated_at) FROM {budget})",
         ("analysis_period_end",)),
        ("forecast", "SELECT * FROM {forecast} WHERE forecast_execution_date = "
                     "(SELECT MAX(forecast_execution_date) FROM {forecast})", ("forecast_execution_date", "target_date")),
    ):
        try:
            data[name] = _dates(q(sql), *cols)
        except Exception as exc:  # noqa: BLE001 — one missing input is a visible gap, not a lost brief
            problems.append(f"couldn't read the {name} table ({str(exc)[:120]})")
    if "flags" in data:
        data["flags"]["confidence"] = data["flags"]["confidence"].fillna("low")
    return data, problems


def run_brief(deps: AgentDeps, model: Model | None, now: datetime | None = None) -> Brief:
    run_id = new_trace_id()
    t, budgets = settings(deps.pack)
    try:
        data, problems = load(deps, t)
    except Exception as exc:  # noqa: BLE001 — without gold there is nothing to say, but the issue says why
        data, problems = {}, [f"couldn't read the ad data ({str(exc)[:160]})"]
    if "gold" not in data:
        md = f"# Daily brief\n\n**No brief today: {problems[0]}.**\n\nbrief run `{run_id}`\n"
        return Brief(run_id, None, [], problems, [], md)

    gold, as_of, latest = data["gold"], data["as_of"], data["latest"]
    warnings, fine, ahead, series = list(problems), [], [], None
    cost, steady = a.cost_items(gold, as_of, t)
    if steady:
        fine.append(f"Cost per sale held steady on {', '.join(steady)}")
    anomalies, noise = a.anomaly_items(gold, data.get("flags", pd.DataFrame(columns=["is_anomaly"])), as_of, t)
    if "flags" in data and noise:
        fine.append(noise)
    if not any(i.id.endswith((":tracking", ":double")) for i in anomalies):
        fine.append("No signs of broken or double-counted tracking")

    fc = data.get("forecast")
    if fc is not None and (fc.empty or fc["forecast_execution_date"].max() < as_of):
        if not fc.empty:
            ahead.append(f"The forecast is stale (made {a.day(fc['forecast_execution_date'].max())}); trend skipped")
        fc = None
    pace_lines, pace, _ = a.pacing(gold, fc, budgets, latest, t)
    ahead += [f"Month-end budget, {line}" for line in pace_lines]
    if fc is not None:
        trend, series = a.outlook(gold, fc, as_of, t)
        ahead += trend

    moves = []
    plan = data.get("plan")
    if plan is not None and not plan.empty:
        if plan["analysis_period_end"].max() < as_of - timedelta(days=1):
            fine.append(f"The optimizer's plan is stale (ends {a.day(plan['analysis_period_end'].max())}); not used")
        elif (m := a.move_item(plan, t)) is not None:
            moves.append(m)

    candidates = cost + anomalies + pace + moves
    today = {i.id: i for i in candidates}
    try:
        stored = [
            {"item_id": r.item_id, "kind": r.kind, "advice": r.advice, "brief_date": r.brief_date,
             "stake": r.at_stake_usd, "check": json.loads(r.check_json or "{}")}
            for r in deps.audit.recent_brief_items(deps.pack.name, as_of - timedelta(days=t["followup_days"] + 7))
            if r.status == "open"
        ]
    except Exception as exc:  # noqa: BLE001 — shown and failing the job, never silently skipped
        stored = []
        problems.append(f"couldn't load past advice ({str(exc)[:120]})")
        warnings.append(problems[-1])
    followed = a.follow_up(stored, today, gold, as_of, latest, t)
    items, rest = a.rank(candidates, followed, t)
    fine += [f"Smaller: {i.title} (about {a.money(i.stake)})" for i in rest[:3]]
    if len(rest) > 3:
        fine.append(f"…and {len(rest) - 3} more, {a.under(rest[3].stake)} each")

    text, model_used, caveats = write(deps, model, items, ahead, [f["line"] for f in followed], run_id)
    footer = (f"pack `{deps.pack.name}` · writer `{model_used or 'templates'}` · brief run `{run_id}` "
              f"(`adpilot audit export --run {run_id}`)")
    md, leaked = redact_output(render(as_of=as_of, latest=latest, items=items, text=text, ahead=ahead, series=series,
                                      followed=[f["line"] for f in followed], fine=fine, warnings=warnings, footer=footer))
    if leaked:
        caveats.append("OutputPolicy")

    ts = now or datetime.now(UTC)
    first = {f["item_id"]: f["brief_date"] for f in followed}
    rows = [BriefItemRow(ts=ts, run_id=run_id, pack=deps.pack.name, brief_date=first.get(i.id, as_of), item_id=i.id,
                         kind=i.kind, subject=i.subject, advice=text[i.id]["title"], at_stake_usd=round(i.stake, 2),
                         check_json=json.dumps(i.check), status="open") for i in items]
    published = {r.item_id for r in rows}
    rows += [BriefItemRow(ts=ts, run_id=run_id, pack=deps.pack.name, brief_date=f["brief_date"], item_id=f["item_id"],
                          kind=f["kind"], subject="", advice=f["advice"], at_stake_usd=round(f["stake"], 2),
                          check_json=json.dumps(f["check"]), status=f["status"])
             for f in followed if f["item_id"] not in published]
    deps.audit.record_brief_items(rows)
    return Brief(run_id, as_of, items, problems, caveats, md, rows)
