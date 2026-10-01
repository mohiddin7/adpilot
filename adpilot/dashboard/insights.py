"""GET /insights: the daily brief's analyses plus three window-vs-window ones over the viewer's window, money lost
first and then by the dollars involved, one card per campaign, each card with an evidence chart and a "why"
(which rate or which campaigns drove it) computed here. Every query goes through execute(), so the SQL
policy applies; numbers are code's, and a free model may only reword the title, action and why through the brief's
grounded writer (a number it was not given sends that slot back to the template)."""

from __future__ import annotations

import calendar
import dataclasses
import logging
from datetime import date, timedelta

import pandas as pd
from pydantic_ai.models import Model

from adpilot.brief import analyses as a
from adpilot.brief import settings
from adpilot.brief.writer import write
from adpilot.core.audit import new_trace_id
from adpilot.core.chart import PanelChartSpec
from adpilot.core.runtime import fresh_deps
from adpilot.core.tools import AgentDeps, SqlError, execute
from adpilot.dashboard.config import DashboardConfig
from adpilot.dashboard.filters import Filters, build_where
from adpilot.dashboard.panels import TtlCache, _safe_error, dashboard_meta

log = logging.getLogger(__name__)

MAX_CARDS = 8
MAX_ROWS = 20_000  # campaign-days over both windows; a 366-day window on the live data is about 8K
FACTS_MAX = 400
GOLD_SQL = ("SELECT date, platform, campaign_id, campaign_name, SUM(impressions) AS impressions, SUM(clicks) AS clicks,"
            " SUM(spend) AS spend, SUM(conversions) AS conversions FROM {gold} WHERE {where}"
            " GROUP BY date, platform, campaign_id, campaign_name")
FLAGS_SQL = "SELECT * FROM {anomalies} WHERE {where} AND is_anomaly = 1"
PLAN_SQL = "SELECT * FROM {budget} WHERE generated_at = (SELECT MAX(generated_at) FROM {budget})"
FORECAST_SQL = ("SELECT * FROM {forecast} WHERE forecast_execution_date = "
                "(SELECT MAX(forecast_execution_date) FROM {forecast})")


def _frame(template: AgentDeps, cfg: DashboardConfig, table: str, sql: str, flt: Filters | None,
           dates: tuple[str, ...]) -> tuple[pd.DataFrame | None, str | None]:
    """(frame, None) or (None, why): `why` is a fixed sentence; the connector's text goes to the log only."""
    deps = fresh_deps(template)
    dialect = deps.connector.dialect
    where = build_where(cfg, table, deps.pack.raw["date_column"], flt, dialect) if flt else "1 = 1"
    res = execute(deps, deps.pack.render(sql, dialect, where=where), max_rows=MAX_ROWS + 1, max_bytes=cfg.max_bytes_billed)
    if isinstance(res, SqlError):
        _safe_error(res.kind, res.message)  # logs it
        return None, f"couldn't read the {table} table"
    # validate_sql caps the LIMIT at max_rows, so res.truncated never fires: one row past MAX_ROWS is the signal.
    if len(res.rows) > MAX_ROWS:  # an arbitrary subset of the rows would publish wrong dollar numbers
        log.warning("insights: %s truncated at %d rows", table, MAX_ROWS)
        return None, "the window is too long to analyse in full; narrow the dates"
    df = pd.DataFrame(res.rows, columns=res.columns)
    for c in dates:
        df[c] = pd.to_datetime(df[c]).dt.date
    return df, None


def _severity(stake: float, spend: float) -> str:
    share = stake / spend if spend > 0 else 0.0
    return "high" if share >= 0.05 else "medium" if share >= 0.01 else "low"


def _stake_label(i: a.Item) -> str:
    """What the stake is. Only "at stake" is money lost or overpaid: a spend mover is a change, a move or mix is
    reallocatable, and a cost per sale that fell (or a day cheaper than usual) is an opportunity."""
    if i.kind in ("move", "mix"):
        return "to reallocate"
    if i.kind == "mover" and i.id.endswith(":spend"):
        return "change in spend"
    cheaper = (i.kind == "mover" and i.check["after"] < i.check["before"]) or (i.kind == "anomaly" and i.check["kind"] == "low")
    return "opportunity" if cheaper else "at stake"


def _one_per_campaign(ranked: list[a.Item]) -> tuple[list[a.Item], dict[str, list[str]]]:
    """Keep the first (best-ranked) finding per (platform, campaign id); a later one's sentence goes to the kept
    card's `also`. Only the campaign-level kinds have that key, in `check`."""
    kept, first, also = [], {}, {}
    for i in ranked:
        key = (i.check["platform"], i.check["campaign_id"]) if i.kind in ("anomaly", "mover", "outlier") else None
        if key in first:
            also.setdefault(first[key], []).append(i.happened)
            continue
        if key:
            first[key] = i.id
        kept.append(i)
    return kept, also


def _chart(spec: dict, rows: list[dict], formats: dict) -> dict | None:
    return {"spec": PanelChartSpec(**spec).model_dump(), "rows": rows, "formats": formats} if rows else None


def _daily_cpa(g: pd.DataFrame, end: date, days: int) -> pd.DataFrame:
    g = g[(g["date"] > end - timedelta(days=days)) & (g["date"] <= end)].groupby("date")[["spend", "conversions"]].sum()
    g = g[g["conversions"] > 0]
    return g.assign(cpa=(g["spend"] / g["conversions"]).round(2)).reset_index()


def _evidence(i: a.Item, gold: pd.DataFrame, cur: pd.DataFrame, plan: pd.DataFrame | None, budgets: dict,
              as_of: date, latest: date) -> dict | None:
    c = i.check
    if i.kind == "cost":
        d = _daily_cpa(gold[gold["platform"] == c["platform"]], as_of, 14)
        rows = [{"date": r.date.isoformat(), "week": "this week" if r.date > as_of - timedelta(days=7) else "last week",
                 "cpa": r.cpa} for r in d.itertuples()]
        return _chart({"chart_type": "line", "x": "date", "y": "cpa", "color": "week"}, rows, {"cpa": "currency"})
    if i.kind == "anomaly":
        g = gold[(gold["platform"] == c["platform"]) & (gold["campaign_id"] == c["campaign_id"])]
        rows = [{"date": r.date.isoformat(), "cpa": r.cpa} for r in _daily_cpa(g, as_of, 21).itertuples()]
        return _chart({"chart_type": "line", "x": "date", "y": "cpa", "reference": "mean_y"}, rows, {"cpa": "currency"})
    if i.kind == "pace":
        g = gold[(gold["platform"] == c["platform"]) & (gold["date"] >= latest.replace(day=1)) & (gold["date"] <= latest)]
        daily = g.groupby("date")["spend"].sum().cumsum()
        ndays = calendar.monthrange(latest.year, latest.month)[1]
        budget = float(budgets.get(c["platform"], 0))
        rows = [{"date": d.isoformat(), "series": "spent", "spend": round(float(v), 2)} for d, v in daily.items()]
        rows += [{"date": d.isoformat(), "series": "budget pace", "spend": round(budget * d.day / ndays, 2)} for d in daily.index]
        return _chart({"chart_type": "line", "x": "date", "y": "spend", "color": "series"}, rows, {"spend": "currency"})
    if i.kind == "move" and plan is not None:
        rows = [{"platform": r.platform, "plan": p, "spend": round(float(v), 2)} for r in plan.itertuples()
                for p, v in (("current", r.current_spend), ("recommended", r.recommended_spend))]
        return _chart({"chart_type": "bar", "x": "platform", "y": "spend", "color": "plan"}, rows, {"spend": "currency"})
    if i.kind == "mover":
        y = c["metric"]  # spend | cpa: the axis is titled from the column name
        rows = [{"period": "previous", y: round(c["before"], 2)}, {"period": "this", y: round(c["after"], 2)}]
        return _chart({"chart_type": "bar", "x": "period", "y": y}, rows, {y: "currency"})
    if i.kind == "outlier" and c["cpa"] is not None:
        rows = [{"name": "this campaign", "cpa": round(c["cpa"], 2)}, {"name": "account average", "cpa": round(c["account"], 2)}]
        return _chart({"chart_type": "bar", "x": "name", "y": "cpa"}, rows, {"cpa": "currency"})
    if i.kind == "mix":
        p = cur.groupby("platform")[["spend", "conversions"]].sum()
        rows = [{"platform": plat, "measure": m, "share": round(float(r[col] / p[col].sum()), 4)}
                for plat, r in p.iterrows() for m, col in (("Share of spend", "spend"), ("Share of sales", "conversions"))]
        return _chart({"chart_type": "bar", "x": "platform", "y": "share", "color": "measure"}, rows, {"share": "percent"})
    return None


def _bars(changes: dict, names: dict) -> dict | None:
    rows = [{"measure": names[k], "change": round(v, 4)} for k, v in changes.items()]
    return _chart({"chart_type": "bar", "x": "measure", "y": "change"}, rows, {"change": "percent"})


def _rate_why(before: dict, after: dict, versus: bool = False) -> dict:
    """Which of ad price, clicks per view and sales per click moved the cost per sale, as a sentence and three bars."""
    sp = a.split(before, after)
    return {"text": a.versus(sp) if versus else a.explain(sp)[0], "chart": _bars(sp["changes"], a.RATES) if sp else None}


def _campaigns_why(cur: pd.DataFrame, plat: str) -> dict | None:
    """The platform's campaigns by what they cost above the account's cost per sale: the top one named, five drawn."""
    found = a.driving_campaigns(cur, plat)
    if found is None:
        return None
    if not found:
        return {"text": f"No {plat} campaign pays more per sale than the account average.", "chart": None}
    (name, top), total = found[0], sum(v for _, v in found)
    rows = [{"campaign_name": n, "excess_cost": round(v, 2)} for n, v in found[:5]]
    return {"text": f'"{name}" carries {top / total:.0%} of {plat}\'s cost above the account average '
                    f"({a.money(top)} of {a.money(total)}).",
            "chart": _chart({"chart_type": "bar_h", "x": "campaign_name", "y": "excess_cost"}, rows, {"excess_cost": "currency"})}


def _why(i: a.Item, gold: pd.DataFrame, cur: pd.DataFrame, prev: pd.DataFrame, as_of: date) -> dict | None:
    """Why the finding happened, computed from the frames already loaded: {text, chart} or None. No query, no model."""
    c = i.check
    if i.kind in ("mix", "pace"):
        return _campaigns_why(cur, c["platform"])
    if i.kind == "cost":  # the same two weeks cost_items compared
        g = gold[gold["platform"] == c["platform"]]
        why = _rate_why(a._sums(a._window(g, as_of - timedelta(days=7), 7)), a._sums(a._window(g, as_of, 7)))
        top = _campaigns_why(cur, c["platform"])
        return {**why, "text": f"{why['text']} {top['text']}"} if top else why
    if i.kind not in ("anomaly", "mover", "outlier"):
        return None  # a budget move: the card already shows each platform's cost per sale

    def mine(df: pd.DataFrame) -> dict:
        return a._sums(df[(df["platform"] == c["platform"]) & (df["campaign_id"] == c["campaign_id"])])

    if i.kind == "outlier":
        return _rate_why(a._sums(cur), mine(cur), versus=True)
    if i.kind == "mover" and c["metric"] == "spend":
        sp = a.spend_split(mine(prev), mine(cur))
        return {"text": a.explain_spend(sp),
                "chart": _bars(sp["changes"], {"views": "Views", "price": "Price per view"}) if sp else None}
    return _rate_why(mine(prev), mine(cur))


def run_insights(template: AgentDeps, cfg: DashboardConfig, flt: Filters, model: Model | None, cache: TtlCache) -> dict:
    key = ("insights", flt)
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        t, budgets = settings(template.pack)
    except ValueError as exc:  # our own text about pack.yaml: safe to show
        return {"cards": [], "checked": [], "problems": [str(exc)], "writer": "templates", "at_stake": 0.0}
    meta = dashboard_meta(template, cfg, cache)  # AdPilotError → the route's 503
    first, latest = date.fromisoformat(meta["date_min"]), date.fromisoformat(meta["date_max"])
    chosen = flt.values("platform")
    if chosen:
        budgets = {k: v for k, v in budgets.items() if k in chosen}
    days = (flt.date_to - flt.date_from).days + 1
    prev_from = flt.date_from - timedelta(days=days)
    as_of = min(flt.date_to, latest - timedelta(days=t["mature_lag_days"]))
    since = min(prev_from, as_of - timedelta(days=21), latest.replace(day=1))
    problems: list[str] = []
    gold, why = _frame(template, cfg, "gold", GOLD_SQL,
                       dataclasses.replace(flt, date_from=since, ranges=()), ("date",))
    if gold is None:
        return {"cards": [], "checked": [], "problems": [why], "writer": "templates", "at_stake": 0.0}
    cur = gold[(gold["date"] >= flt.date_from) & (gold["date"] <= flt.date_to)]
    if cur.empty:
        return {"cards": [], "checked": [], "problems": ["There is no ad data in this period."], "writer": "templates",
                "at_stake": 0.0}
    prev = gold[(gold["date"] >= prev_from) & (gold["date"] < flt.date_from)]

    week = dataclasses.replace(flt, date_from=as_of - timedelta(days=6), date_to=as_of, ranges=())
    flags, why = _frame(template, cfg, "anomalies", FLAGS_SQL, week, ("date",))
    if flags is None:
        problems.append(why)
    else:
        flags["confidence"] = flags["confidence"].fillna("low")
    plan = fc = None
    if not chosen:  # the optimizer's plan is whole-account; a one-platform view has no move to make
        plan, why = _frame(template, cfg, "budget", PLAN_SQL, None, ("analysis_period_end",))
        problems += [why] if plan is None else []
        if plan is not None and (plan.empty or plan["analysis_period_end"].max() < as_of - timedelta(days=1)):
            plan = None
    current = flt.date_to >= latest  # pacing is about this month: only when the window reaches the newest day
    if current:
        fc, why = _frame(template, cfg, "forecast", FORECAST_SQL, None, ("forecast_execution_date", "target_date"))
        problems += [why] if fc is None else []
        if fc is not None and (fc.empty or fc["forecast_execution_date"].max() < as_of):
            fc = None

    checks = [
        ("cost per sale", lambda: a.cost_items(gold, as_of, t)[0]),
        ("unusual days and tracking", lambda: a.anomaly_items(gold, flags, as_of, t)[0] if flags is not None else None),
        ("month-end pacing", lambda: a.pacing(gold, fc, budgets, latest, t)[1] if current else None),
        ("budget optimizer", lambda: ([m] if (m := a.move_item(plan, t)) else []) if plan is not None else None),
        ("top movers", lambda: a.top_movers(cur, prev, t) if prev_from >= first else None),  # no data before `first`
        ("efficiency outliers", lambda: a.efficiency_outliers(cur, t)),
        ("channel mix", lambda: a.mix_gaps(cur, t) if not chosen or len(chosen) > 1 else None),
    ]
    found: list[a.Item] = []
    checked: list[str] = []
    for name, check in checks:
        try:
            got = check()
        except Exception:  # noqa: BLE001 — one broken analysis is a named gap, never a blank feed
            log.exception("insights: the %s check failed", name)
            problems.append(f"The {name} check could not run.")
            continue
        if got is not None:  # None: the check does not apply to this view
            found += got
            checked.append(name)
    found.sort(key=lambda i: (_stake_label(i) != "at stake", -i.stake))  # money lost first, then by size
    found, also = _one_per_campaign(found)
    top = found[:MAX_CARDS]
    text, model_used, _ = write(fresh_deps(template), model, top, [], [], new_trace_id(), source="dashboard",
                                case_name="insights_writer") if top else ({}, None, None)
    spend = float(cur["spend"].sum())
    cards = []
    for i in top:
        try:
            chart = _evidence(i, gold, cur, plan, budgets, as_of, latest)
        except Exception:  # noqa: BLE001 — a card without its chart beats no card
            log.exception("insights: evidence for %s failed", i.id)
            chart = None
        try:
            why = _why(i, gold, cur, prev, as_of)
        except Exception:  # noqa: BLE001 — a card without its why beats no card
            log.exception("insights: the why for %s failed", i.id)
            why = None
        cards.append({
            "id": i.id, "kind": i.kind, "platform": i.check.get("platform"), "why_detail": why,
            "severity": _severity(i.stake, spend), "stake": round(i.stake, 2),
            "loss": _stake_label(i) == "at stake", "stake_label": _stake_label(i), "also": also.get(i.id, []),
            "title": text[i.id]["title"], "headline": i.happened, "action": text[i.id]["do"],
            "why": text[i.id]["checked"], "confidence": i.confidence, "numbers": i.numbers, "chart": chart,
            "facts": f"{i.happened} {text[i.id]['checked']}"[:FACTS_MAX],
        })
    out = {"cards": cards, "checked": checked, "problems": problems, "writer": model_used or "templates",
           "at_stake": round(sum(c["stake"] for c in cards if c["loss"]), 2)}
    if not problems:
        cache.put(key, out)
    return out
