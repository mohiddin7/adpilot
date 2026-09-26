"""The brief's analyses: pure functions from DataFrames to Items. Code computes and formats every number; the
writer (writer.py) only rephrases a few text slots, and falls back to the templates written here.

`gold` is daily rows per (date, platform, campaign_id, campaign_name) with impressions, clicks, spend and
conversions. `as_of` is the newest date whose conversions are final; `latest` is the newest date with spend.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

NUM = ["impressions", "clicks", "spend", "conversions"]
CONFIDENCE = ("low", "medium", "high")

# driver -> (what moved, likely cause, the fix)
CAUSES = {
    "price": ("ads got pricier", "more competition in the ad auction", "Review bids, or wait a few days for prices to settle."),
    "clicks": ("fewer people clicked", "the ads are wearing out", "Refresh the ad creative."),
    "buyers": ("fewer clickers bought", "a landing page, offer or tracking problem", "Check the landing page, the offer and the conversion tracking."),
}


@dataclass
class Item:
    id: str  # stable across briefs: the follow-up key
    kind: str  # cost | anomaly | pace | move
    subject: str
    stake: float  # USD at stake; the ranking key
    happened: str
    title: str  # title / checked / do are templates the writer may rephrase
    checked: str
    do: str
    confidence: str
    check_line: str
    check: dict = field(default_factory=dict)  # baseline values the follow-up needs
    numbers: list[dict] = field(default_factory=list)  # rows for the collapsed "numbers" section
    per: str = ""  # "a week" when the stake recurs


def money(x: float) -> str:
    x = abs(x)
    if x >= 10_000:
        return f"${x / 1000:,.0f}K"
    if x >= 1000:
        return f"${x / 1000:.1f}K"
    return f"${x:,.0f}" if x >= 100 else f"${x:.2f}"


def under(x: float) -> str:
    return f"under ${math.floor(x) + 1:,}"


def pct(ratio: float) -> str:
    return f"{ratio:+.0%}"


def day(d: date) -> str:
    return f"{d:%b} {d.day}"


def _sums(df: pd.DataFrame) -> dict:
    return {k: float(df[k].sum()) for k in NUM}


def _window(df: pd.DataFrame, end: date, days: int) -> pd.DataFrame:
    return df[(df["date"] > end - timedelta(days=days)) & (df["date"] <= end)]


def split(before: dict, after: dict) -> dict | None:
    """Cost per sale = ad price per view / clicks per view / sales per click, so the log change splits exactly into
    three terms that sum to ln(CPA_after / CPA_before). None when a rate is undefined."""
    if any(s[k] <= 0 for s in (before, after) for k in NUM):
        return None

    def rates(s):
        return s["spend"] / s["impressions"], s["clicks"] / s["impressions"], s["conversions"] / s["clicks"]

    (p0, c0, b0), (p1, c1, b1) = rates(before), rates(after)
    terms = {"price": math.log(p1 / p0), "clicks": -math.log(c1 / c0), "buyers": -math.log(b1 / b0)}
    return {"terms": terms, "total": sum(terms.values()),
            "changes": {"price": p1 / p0 - 1, "clicks": c1 / c0 - 1, "buyers": b1 / b0 - 1}}


def drivers(sp: dict) -> list[str]:
    """The term that carries at least half the change, else the top two pushing the same way."""
    total = sp["total"]
    same = sorted((k for k, v in sp["terms"].items() if v * total > 0), key=lambda k: -abs(sp["terms"][k]))
    if not same:
        return []
    return same[:1] if sp["terms"][same[0]] / total >= 0.5 else same[:2]


def explain(sp: dict | None) -> tuple[str, str]:
    """(checked, do) templates from a split."""
    if sp is None or not drivers(sp):
        return "The change has no single clear cause in ad price, clicks or sales per click.", "Look at the campaign's recent changes."
    ch, ds = sp["changes"], drivers(sp)
    what = " and ".join(CAUSES[d][0] for d in ds)
    cause = " or ".join(CAUSES[d][1] for d in ds)
    return (f"Ad price {pct(ch['price'])}, clicks per view {pct(ch['clicks'])}, sales per click {pct(ch['buyers'])}. "
            f"Mostly {what}, which points to {cause}.", " ".join(CAUSES[d][2] for d in ds))


# ---------- 1. cost changes by platform ----------


def cost_items(gold: pd.DataFrame, as_of: date, t: dict) -> tuple[list[Item], list[str]]:
    """Items for platforms whose cost per sale rose, and the names of platforms that held steady."""
    items, steady = [], []
    for plat, g in gold.groupby("platform"):
        w1, w0 = _sums(_window(g, as_of, 7)), _sums(_window(g, as_of - timedelta(days=7), 7))
        if min(w0["conversions"], w1["conversions"]) < t["min_conversions"]:
            continue
        cpa0, cpa1 = w0["spend"] / w0["conversions"], w1["spend"] / w1["conversions"]
        if cpa1 / cpa0 - 1 < t["cost_rise_pct"] / 100:
            steady.append(plat)
            continue
        checked, do = explain(split(w0, w1))
        items.append(Item(
            id=f"cost:{plat}", kind="cost", subject=plat, stake=(cpa1 - cpa0) * w1["conversions"],
            happened=f"{plat} paid {money(cpa1)} per sale this week, up {pct(cpa1 / cpa0 - 1)} from {money(cpa0)}.",
            title=f"Bring {plat}'s cost per sale back down", checked=checked, do=do,
            confidence=f"{'high' if w1['conversions'] >= 100 else 'medium'} ({w1['conversions']:,.0f} sales this week)",
            check_line=f"whether {plat}'s cost per sale falls back toward {money(cpa0)}",
            check={"platform": plat, "cpa0": cpa0},
            numbers=[{"week": wk, "spend": money(w["spend"]), "sales": f"{w['conversions']:,.0f}",
                      "cost per sale": money(w["spend"] / w["conversions"])} for wk, w in (("last", w0), ("this", w1))],
        ))
    return items, steady


# ---------- 2. anomaly triage ----------


def _campaign_days(gold: pd.DataFrame) -> pd.DataFrame:
    return gold.groupby(["platform", "campaign_id", "campaign_name", "date"], as_index=False)[NUM].sum()


def anomaly_items(gold: pd.DataFrame, flags: pd.DataFrame, as_of: date, t: dict) -> tuple[list[Item], str | None]:
    """Tracking faults and double counts found in gold (flagged or not), plus the detector's flags this week.
    Returns the items that need the reader, and one summary line for the rest."""
    week = [as_of - timedelta(days=i) for i in range(6, -1, -1)]
    groups: dict[tuple, list[dict]] = {}
    shape: dict[tuple, str] = {}  # (platform, campaign_id, date) -> tracking | double
    daily = _campaign_days(gold)
    by_campaign = {k: g.set_index("date") for k, g in daily.groupby(["platform", "campaign_id", "campaign_name"])}

    def base(key, d):
        g = by_campaign.get(key)
        if g is None or d not in g.index:
            return None, None
        prior = g[(g.index >= d - timedelta(days=14)) & (g.index < d)]
        return g.loc[d], (prior if len(prior) >= 7 else None)

    for key in by_campaign:
        for d in week:
            row, prior = base(key, d)
            if row is None or prior is None:
                continue
            med = prior[NUM].median()
            if med["conversions"] < t["tracking_min_daily_sales"] or med["clicks"] <= 0:
                continue
            if abs(row["clicks"] / med["clicks"] - 1) > t["clicks_normal_band"]:
                continue
            ratio = row["conversions"] / med["conversions"]
            kind = "tracking" if ratio <= t["tracking_drop_ratio"] else "double" if ratio >= t["double_count_ratio"] else None
            if kind:
                shape[(key[0], key[1], d)] = kind
                base_cpa = prior["spend"].sum() / max(prior["conversions"].sum(), 1)
                groups.setdefault((*key, kind), []).append({
                    "date": d, "spend": row["spend"], "sales": row["conversions"], "clicks": row["clicks"],
                    "normal_sales": med["conversions"], "normal_clicks": med["clicks"], "normal_spend": med["spend"],
                    "cpa": row["spend"] / row["conversions"] if row["conversions"] else None, "normal_cpa": base_cpa,
                    "stake": abs(row["spend"] - base_cpa * row["conversions"]), "confidence": "medium", "split": None})

    for f in flags.itertuples():
        if not f.is_anomaly or f.date not in week or (f.platform, f.campaign_id, f.date) in shape:
            continue
        key = (f.platform, f.campaign_id, f.campaign_name)
        row, prior = base(key, f.date)
        sales = row["conversions"] if row is not None else 0.0
        kind = "high" if f.anomaly_direction == "HIGH_CPA" else "low"
        groups.setdefault((*key, kind), []).append({
            "date": f.date, "spend": row["spend"] if row is not None else f.observed_cpa * sales, "sales": sales,
            "clicks": row["clicks"] if row is not None else 0.0, "normal_spend": prior["spend"].median() if prior is not None else 0.0,
            "cpa": f.observed_cpa, "normal_cpa": f.rolling_mean_cpa, "stake": abs(f.observed_cpa - f.rolling_mean_cpa) * sales,
            "confidence": f.confidence or "low",
            "split": split(_sums(prior), {k: float(row[k]) for k in NUM}) if row is not None and prior is not None else None})

    items, noise = [], []
    recent = {as_of - timedelta(days=i) for i in range(3)}
    for (plat, cid, name, kind), days in groups.items():
        stake = sum(x["stake"] for x in days)
        dates = {x["date"] for x in days}
        persistent = as_of in dates or len(dates & recent) >= 2
        if kind in ("high", "low") and not (persistent and stake >= t["anomaly_persist_usd"]) and stake < t["anomaly_any_usd"]:
            noise.append(stake)
            continue
        items.append(_anomaly_item(plat, cid, name, kind, days, stake))
    line = None
    if noise:
        line = f"{len(noise)} other anomaly flag{'s' if len(noise) > 1 else ''}: small ({under(max(noise))} each) or one-day"
    return items, line


def _anomaly_item(plat, cid, name, kind, days, stake) -> Item:
    worst = max(days, key=lambda x: x["stake"])
    when = day(worst["date"])
    more = f" It happened on {len(days)} days this week." if len(days) > 1 else ""
    conf = min((x["confidence"] for x in days), key=lambda c: CONFIDENCE.index(c) if c in CONFIDENCE else 0)
    subject = f'{plat} campaign "{name}"'
    if kind == "tracking":
        happened = (f"On {when} it got {worst['sales']:,.0f} sales from {worst['clicks']:,.0f} clicks; normally about "
                    f"{worst['normal_sales']:,.0f} sales from {worst['normal_clicks']:,.0f} clicks.")
        title, check_line = f"Check the tracking on {subject}", "whether its sales come back"
        checked = "Clicks held steady but sales fell by half or more, which points to broken tracking or a broken landing page, not lost demand."
        do = "Check the campaign's conversion tracking and landing page; pause it if either is broken."
    elif kind == "double":
        happened = (f"On {when} it reported {worst['sales']:,.0f} sales from {worst['clicks']:,.0f} clicks; normally about "
                    f"{worst['normal_sales']:,.0f} sales.")
        title, check_line = f"Check {subject} for double-counted sales", "whether its reported sales go back to normal"
        checked = "Clicks were normal but reported sales jumped several times over, which usually means sales are counted twice."
        do = "Look for a duplicate conversion tag before trusting this campaign's results or giving it more budget."
    else:
        happened = f"On {when} it paid {money(worst['cpa'])} per sale; its normal is about {money(worst['normal_cpa'])}."
        checked, do = explain(worst["split"])
        if kind == "high":
            title, check_line = f"Look into {subject}", "whether its cost per sale returns to normal"
        else:
            title, check_line = f"Consider more budget for {subject}", "whether it stays this cheap"
            do = "If it stays this cheap for another day, give it more budget."
    return Item(
        id=f"anomaly:{plat}:{cid}:{kind}", kind="anomaly", subject=subject, stake=stake, happened=happened + more,
        title=title, checked=checked, do=do,
        confidence=f"{conf} ({'one day' if len(days) == 1 else f'{len(days)} days this week'})", check_line=check_line,
        check={"platform": plat, "campaign_id": cid, "kind": kind, "normal_sales": float(worst.get("normal_sales") or 0),
               "normal_spend": float(worst.get("normal_spend") or 0)},
        numbers=[{"date": day(x["date"]), "spend": money(x["spend"]), "sales": f"{x['sales']:,.0f}",
                  "cost per sale": money(x["cpa"]) if x["cpa"] else "–", "normal cost per sale": money(x["normal_cpa"])}
                 for x in sorted(days, key=lambda x: x["date"])],
    )


# ---------- 3. pacing ----------


def pacing(gold: pd.DataFrame, forecast: pd.DataFrame | None, budgets: dict, latest: date, t: dict
           ) -> tuple[list[str], list[Item], dict]:
    """Projected month-end spend vs the monthly budget. Days past the forecast use the last-7-day run rate."""
    start = latest.replace(day=1)
    end = latest.replace(day=calendar.monthrange(latest.year, latest.month)[1])
    left = [latest + timedelta(days=i) for i in range(1, (end - latest).days + 1)]
    lines, items, status = [], [], {}
    for plat in sorted(set(gold["platform"]) | set(budgets)):
        g = gold[gold["platform"] == plat]
        if plat not in budgets:
            lines.append(f"{plat}: no budget set")
            continue
        mtd = float(g[(g["date"] >= start) & (g["date"] <= latest)]["spend"].sum())
        rate = float(_window(g, latest, 7)["spend"].sum()) / 7
        fc = {}
        if forecast is not None and len(forecast):
            f = forecast[(forecast["platform"] == plat) & (forecast["metric_name"] == "spend")]
            fc = dict(zip(f["target_date"], f["predicted_value"], strict=True))
        projected = mtd + sum(fc.get(d, rate) for d in left)
        budget = float(budgets[plat])
        off = projected / budget - 1
        status[plat] = off
        if abs(off) <= t["pacing_on_track_pct"] / 100:
            lines.append(f"{plat}: on track")
            continue
        over = off > 0
        lines.append(f"{plat}: will {'overspend' if over else 'underspend'} by about {money(projected - budget)}")
        if not over or off < t["pacing_item_pct"] / 100:
            continue  # unspent budget is not money at risk: it stays a line under "Looking ahead"
        per_day = (budget - mtd) / len(left) if left else 0.0
        do = (f"Lower {plat}'s daily budget to about {money(per_day)} a day for the rest of the month."
              if per_day > 0 else f"{plat} has already spent its monthly budget; pause or cut it for the rest of the month.")
        items.append(Item(
            id=f"pace:{plat}:{latest:%Y-%m}", kind="pace", subject=plat, stake=abs(projected - budget),
            happened=f"At this pace {plat} will spend about {money(projected)} this month against a {money(budget)} budget.",
            title=f"Slow down {plat} spending",
            checked=f"It has spent {money(mtd)} so far with {len(left)} day{'s' if len(left) != 1 else ''} to go; the rest is projected from "
                    f"{'the forecast' if fc else 'the last 7 days'}.",
            do=do, confidence="high" if fc else "medium (no forecast, run rate only)",
            check_line="whether the month-end projection gets back on budget",
            check={"platform": plat, "month": f"{latest:%Y-%m}"},
            numbers=[{"budget": money(budget), "spent so far": money(mtd), "projected": money(projected), "off by": pct(off)}],
        ))
    return lines, items, status


# ---------- 4. looking ahead ----------


def outlook(gold: pd.DataFrame, forecast: pd.DataFrame, as_of: date, t: dict) -> tuple[list[str], dict | None]:
    """Forecast cost per sale for the next 14 days vs the last 14. Returns lines and the chart series of the platform
    that moves most."""
    lines, unsure, best = [], [], None
    for plat, f in forecast[forecast["target_date"] > as_of].groupby("platform"):
        dates = sorted(set(f["target_date"]))[:14]
        f = f[f["target_date"].isin(dates)]
        sp, cv = f[f["metric_name"] == "spend"], f[f["metric_name"] == "conversions"]
        past = _window(gold[gold["platform"] == plat], as_of, 14)
        if cv["predicted_value"].sum() <= 0 or past["conversions"].sum() <= 0:
            continue
        fc_cpa = sp["predicted_value"].sum() / cv["predicted_value"].sum()
        now_cpa = past["spend"].sum() / past["conversions"].sum()
        change = fc_cpa / now_cpa - 1
        # ponytail: summed daily bounds overstate the range of a 14-day total; fine for a "too uncertain" call
        width = (cv["upper_bound"].sum() - cv["lower_bound"].sum()) / cv["predicted_value"].sum()
        if width > t["outlook_uncertain_ratio"]:
            unsure.append(plat)
            continue
        worse = t["outlook_worse_pct"] / 100
        trend = "getting pricier" if change >= worse else "getting cheaper" if change <= -worse else "flat"
        lines.append(f"{plat}: cost per sale {trend} ({money(now_cpa)} now, about {money(fc_cpa)} forecast)")
        if best is None or abs(change) > abs(best["change"]):
            actual = past.groupby("date")[["spend", "conversions"]].sum()
            ahead = f.pivot_table(index="target_date", columns="metric_name", values="predicted_value", aggfunc="sum")
            best = {"platform": plat, "change": change,
                    "points": [(d, s / c) for d, s, c in zip(actual.index, actual["spend"], actual["conversions"], strict=True) if c > 0]
                    + [(d, r["spend"] / r["conversions"]) for d, r in ahead.iterrows() if r.get("conversions", 0) > 0],
                    "split": day(as_of)}
    if unsure:
        lines.append(f"{'The forecast is' if not lines else ', '.join(unsure) + ':'} too uncertain to call a direction"
                     + (" for any platform" if not lines else ""))
    return lines, best


# ---------- 5. budget move as a staged test ----------


def move_item(plan: pd.DataFrame, t: dict) -> Item | None:
    delta = plan["recommended_spend"] - plan["current_spend"]
    total = float(plan["current_spend"].sum())
    moved = float(delta.clip(lower=0).sum())
    if total <= 0 or moved / total <= t["move_min_share_pct"] / 100:
        return None
    src, dst = plan.loc[delta.idxmin()], plan.loc[delta.idxmax()]
    slice_ = max(1000.0, round(moved * t["move_slice_pct"] / 100 / 1000) * 1000)
    stop = float(dst["current_cpa"]) * t["move_stop_ratio"]
    account_cpa = total / float(plan["current_conversions"].sum())
    weekly = float(plan["conversion_delta"].sum()) * 7 / 30 * account_cpa
    return Item(
        id=f"move:{src['platform']}>{dst['platform']}", kind="move", subject=f"{src['platform']} to {dst['platform']}",
        stake=max(weekly, 0.0), per="a week",
        happened=(f"The optimizer wants to move about {money(moved)} a month from {src['platform']} to {dst['platform']}; "
                  f"{src['platform']} pays {money(src['current_cpa'])} per sale, {dst['platform']} {money(dst['current_cpa'])}."),
        title=f"Test moving {money(slice_)} from {src['platform']} to {dst['platform']}",
        checked=(f"The optimizer assumes {dst['platform']} stays this cheap at any spend. It won't, so move a slice first "
                 "and watch before moving the rest."),
        do=f"Move {money(slice_)} now; stop if {dst['platform']}'s cost per sale goes above {money(stop)}.",
        confidence="medium (the optimizer assumes cost per sale does not change with spend)",
        check_line=f"{dst['platform']}'s cost per sale over the next 7 days",
        check={"from": src["platform"], "to": dst["platform"], "slice_share": slice_ / total, "stop": stop,
               "from_share": float(src["current_spend"]) / total},
        numbers=[{"platform": r.platform, "spend now": money(r.current_spend), "recommended": money(r.recommended_spend),
                  "cost per sale": money(r.current_cpa)} for r in plan.itertuples()],
    )


# ---------- follow-up on past advice ----------


def _share(gold: pd.DataFrame, plat: str, as_of: date) -> float:
    w = _window(gold, as_of, 7)
    total = w["spend"].sum()
    return float(w[w["platform"] == plat]["spend"].sum() / total) if total else 0.0


def _week_cpa(gold: pd.DataFrame, plat: str, as_of: date) -> float | None:
    w = _sums(_window(gold[gold["platform"] == plat], as_of, 7))
    return w["spend"] / w["conversions"] if w["conversions"] else None


def follow_up(stored: list[dict], today: dict[str, Item], gold: pd.DataFrame, as_of: date, latest: date, t: dict
              ) -> list[dict]:
    """Re-evaluate the open items of earlier briefs. Each result carries the row to store and a line to show."""
    out = []
    for s in stored:
        first = s["brief_date"]
        if first >= as_of:
            continue  # nothing new to judge yet
        n = (as_of - first).days
        c = s["check"]
        status, outcome = "open", None
        if n > t["followup_days"]:
            status, outcome = "expired", f"no longer tracked after {t['followup_days']} days"
        elif s["kind"] == "anomaly":
            camp = _campaign_days(gold)
            camp = camp[(camp["platform"] == c["platform"]) & (camp["campaign_id"] == c["campaign_id"]) & (camp["date"] > first)]
            if len(camp) and c["normal_spend"] and camp["spend"].iloc[-1] <= 0.05 * c["normal_spend"]:
                status, outcome = "done", "looks done: the campaign's spend has stopped"
            elif s["item_id"] not in today and len(camp):
                status, outcome = "resolved", "it did not come back" if c["kind"] != "tracking" else "sales came back"
        elif s["kind"] == "cost":
            cpa = _week_cpa(gold, c["platform"], as_of)
            if s["item_id"] not in today and cpa is not None and cpa <= c["cpa0"] * 1.05:
                status, outcome = "resolved", f"cost per sale is back to about {money(cpa)}"
        elif s["kind"] == "pace":
            if c["month"] != f"{latest:%Y-%m}":
                status, outcome = "expired", "the month is over"
            elif s["item_id"] not in today:
                status, outcome = "resolved", "back on budget"
        elif s["kind"] == "move":
            if _share(gold, c["from"], as_of) <= c["from_share"] - c["slice_share"] / 2:
                status, outcome = "done", "looks done: the budget has moved"
                cpa = _week_cpa(gold, c["to"], as_of)
                if cpa is not None and cpa > c["stop"]:
                    outcome += f"; stop, {c['to']}'s cost per sale rose to {money(cpa)}, above {money(c['stop'])}"
        stake = today[s["item_id"]].stake if s["item_id"] in today else s["stake"]
        if outcome is None:
            outcome = f"not done yet (day {n}); about {money(stake)} at stake"
        out.append({**s, "status": status, "outcome": outcome, "stake": stake, "stored_stake": s["stake"],
                    "line": f"{day(first)} · \"{s['advice']}\" → {outcome}"})
    return out


def rank(candidates: list[Item], followed: list[dict], t: dict) -> tuple[list[Item], list[Item]]:
    """Top items by stake. An item still open from an earlier brief stays in "following up" unless its stake grew."""
    stored = {f["item_id"]: f for f in followed if f["status"] == "open"}
    fresh = [i for i in candidates if i.id not in stored or i.stake > stored[i.id]["stored_stake"]]
    fresh.sort(key=lambda i: -i.stake)
    return fresh[: t["max_items"]], fresh[t["max_items"]:]
