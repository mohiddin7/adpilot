"""Calibrated synthetic ad data for the daily run (Phase 3D).

Continues the real Jan-2024 exports in data/raw day by day. Levels come from those exports; the seasonal shape
comes from the public GA360 sample (a real store, 2016-08-01 … 2017-08-01). Both are reduced once to
calibration/levels.json and calibration/seasonality.json, so the deployed functions need neither data/raw nor
BigQuery to generate. Planted CPA anomalies are the detector's ground truth.

  python pipelines/00_generate_synthetic.py --levels        # rebuild levels.json from data/raw (offline)
  python pipelines/00_generate_synthetic.py --seasonality   # rebuild seasonality.json (BigQuery, ~20 MB)
"""
from __future__ import annotations

import argparse
import json
import zlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE.parent / "data" / "raw"          # read only by --levels and the levels test
CALIBRATION_DIR = HERE / "calibration"
SEASONALITY_PATH = CALIBRATION_DIR / "seasonality.json"
LEVELS_PATH = CALIBRATION_DIR / "levels.json"

SEASONALITY_SQL = (
    "SELECT PARSE_DATE('%Y%m%d', date) AS date, IFNULL(SUM(totals.transactions), 0) AS transactions "
    "FROM `bigquery-public-data.google_analytics_sample.ga_sessions_*` GROUP BY 1 ORDER BY 1"
)
EVENTS = ("black_friday", "cyber_monday", "shipping_peak", "christmas_eve", "christmas", "new_years_eve", "new_years_day")

PLATFORMS = {
    "facebook": {"csv": "01_facebook_ads.csv", "out": "facebook_ads.csv", "label": "Facebook",
                 "sub": "ad_set", "cost": "spend", "ids": ("fb_", "fbset_")},
    "google": {"csv": "02_google_ads.csv", "out": "google_ads.csv", "label": "Google",
               "sub": "ad_group", "cost": "cost", "ids": ("g_", "gad_")},
    "tiktok": {"csv": "03_tiktok_ads.csv", "out": "tiktok_ads.csv", "label": "TikTok",
               "sub": "adgroup", "cost": "cost", "ids": ("tt_", "ttad_")},
}


@lru_cache(maxsize=None)
def retail_events(year: int) -> dict[str, tuple[date, ...]]:
    """Each retail event's dates in `year`, computed so the 2016 shape lands on any year's calendar."""
    nov1 = date(year, 11, 1)
    thanksgiving = nov1 + timedelta(days=(3 - nov1.weekday()) % 7 + 21)
    return {
        "black_friday": (thanksgiving + timedelta(days=1),),
        "cyber_monday": (thanksgiving + timedelta(days=4),),
        "shipping_peak": tuple(date(year, 12, d) for d in range(8, 17) if date(year, 12, d).weekday() < 5),
        "christmas_eve": (date(year, 12, 24),),
        "christmas": (date(year, 12, 25),),
        "new_years_eve": (date(year, 12, 31),),
        "new_years_day": (date(year, 1, 1),),
    }


def derive_seasonality(daily: pd.DataFrame) -> dict:
    """daily: one row per day with `date` and `transactions`. Weekday index, 14-day smoothed day-of-year index,
    and each retail event's residual against both."""
    s = daily.assign(date=pd.to_datetime(daily["date"])).set_index("date")["transactions"].astype(float)
    s = s / s.mean()
    dow = s.groupby(s.index.dayofweek).mean()
    dow = dow / dow.mean()
    weekday = s.index.dayofweek.map(dow).to_numpy()
    smooth = (s / weekday).rolling(14, center=True, min_periods=1).mean()
    doy = smooth.groupby(smooth.index.dayofyear).mean().reindex(range(1, 367)).interpolate().bfill().ffill()
    resid = s / (smooth * weekday)
    events = {}
    for name in EVENTS:
        days = [pd.Timestamp(d) for y in sorted(set(s.index.year)) for d in retail_events(y)[name]
                if pd.Timestamp(d) in resid.index]
        events[name] = round(float(resid[days].mean()), 3) if days else 1.0
    return {"doy": [round(float(v), 4) for v in doy], "dow": [round(float(v), 4) for v in dow], "events": events}


def _r(v):
    return [round(float(x), 6) for x in v] if isinstance(v, list) else round(float(v), 6)


def derive_levels() -> dict:
    """Per-campaign daily means and ratios from the real exports, plus each export's exact header."""
    out = {}
    for platform, p in PLATFORMS.items():
        df = pd.read_csv(RAW_DIR / p["csv"])
        campaigns = []
        keys = ["campaign_id", "campaign_name", f"{p['sub']}_id", f"{p['sub']}_name"]
        for (cid, cname, sid, sname), g in df.groupby(keys):
            imp, clicks, conv = g.impressions.sum(), g.clicks.sum(), g.conversions.sum()
            lv = {"imp": imp / len(g), "ctr": clicks / imp, "cvr": conv / clicks, "cpm": g[p["cost"]].sum() / imp * 1000}
            if platform == "facebook":
                lv |= {"vv": g.video_views.sum() / imp, "freq": imp / g.reach.sum(), "er": g.engagement_rate.mean()}
            elif platform == "google":
                lv |= {"aov": g.conversion_value.sum() / conv, "qs": int(g.quality_score.mode()[0]),
                       "sis": g.search_impression_share.mean()}
            else:
                w = [g[c].sum() for c in ("video_views", "video_watch_25", "video_watch_50", "video_watch_75", "video_watch_100")]
                lv |= {"vv": w[0] / imp, "q": [w[i + 1] / w[i] for i in range(4)],
                       "like": g.likes.sum() / w[0], "share": g.shares.sum() / w[0], "comment": g.comments.sum() / w[0]}
            campaigns.append({"campaign_id": cid, "campaign_name": cname, "sub_id": sid, "sub_name": sname,
                              "level": {k: _r(v) for k, v in lv.items()}})
        out[platform] = {"header": list(df.columns), "campaigns": campaigns}
    return out


def write_levels() -> dict:
    levels = derive_levels()
    CALIBRATION_DIR.mkdir(exist_ok=True)
    LEVELS_PATH.write_text(json.dumps(levels, indent=1) + "\n")
    return levels


def write_seasonality() -> dict:
    """Needs BigQuery credentials (owner-approved query, ~20 MB of the free tier). Never runs in CI."""
    import common
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=50 * 1024 ** 2)
    daily = common.bq_client().query(SEASONALITY_SQL, job_config=cfg).to_dataframe()
    cal = {"source": SEASONALITY_SQL, "measured": date.today().isoformat(), **derive_seasonality(daily)}
    CALIBRATION_DIR.mkdir(exist_ok=True)
    SEASONALITY_PATH.write_text(json.dumps(cal, indent=1) + "\n")
    return cal


GENERATOR_VERSION = "1"
SEED = 20240131
HISTORY_START = date(2024, 1, 31)       # first synthetic day; data/raw holds 2024-01-01 … 01-30
MATURITY = {1: 0.85, 2: 0.95}           # age in days → share of final conversions reported; final from age 3
DOW_STRENGTH = 0.5                      # GA's corporate-buyer weekend dip is stronger than consumer retail
ACTIVE_P = 0.92                         # the real exports: 110 rows over 120 campaign-days
ANOMALY_P = 1 / 60
MIN_PLANT_CONVERSIONS = 5               # below this, CPA is noise and a plant would test nothing
CREATIVE_DAYS = 90                      # CTR fatigue resets when the creative refreshes
LAUNCH_EVERY_DAYS = 91                  # about one launch and one pause per platform per quarter
NEW_NAMES = ("Seasonal_Push", "Retargeting_Refresh", "Prospecting_Broad", "Lookalike_Expansion", "Promo_Flash", "Always_On")


@lru_cache(maxsize=1)
def _seasonality() -> dict:
    return json.loads(SEASONALITY_PATH.read_text())


@lru_cache(maxsize=1)
def _levels() -> dict:
    return json.loads(LEVELS_PATH.read_text())


def _rng(*parts: str | int) -> np.random.Generator:
    """A stream fixed by `parts`. crc32 for strings, because Python's hash() is salted per process."""
    return np.random.default_rng([SEED, *(zlib.crc32(p.encode()) if isinstance(p, str) else p for p in parts)])


@dataclass(frozen=True)
class Campaign:
    platform: str
    campaign_id: str
    campaign_name: str
    sub_id: str
    sub_name: str
    start: date
    end: date | None = None                                        # last active day; None while running
    level: dict = field(default_factory=dict, compare=False, hash=False)


@lru_cache(maxsize=None)
def roster(platform: str, until: date) -> tuple[Campaign, ...]:
    """Every campaign on `platform` from HISTORY_START to `until`. The real campaigns continue; every
    LAUNCH_EVERY_DAYS one running campaign pauses and a new one launches from a real campaign's level at its own
    scale. A shorter `until` never changes a campaign that started before it."""
    seeds = _levels()[platform]["campaigns"]
    pc, ps = PLATFORMS[platform]["ids"]
    next_c = max(int(s["campaign_id"].removeprefix(pc)) for s in seeds)
    next_s = max(int(s["sub_id"].removeprefix(ps)) for s in seeds)
    running = [Campaign(platform, s["campaign_id"], s["campaign_name"], s["sub_id"], s["sub_name"], HISTORY_START,
                        level=s["level"]) for s in seeds]
    paused = []
    launch = HISTORY_START + timedelta(days=LAUNCH_EVERY_DAYS)
    while launch <= until:
        r = _rng("roster", platform, launch.toordinal())
        paused.append(replace(running.pop(int(r.integers(len(running)))), end=launch - timedelta(days=1)))
        template = seeds[int(r.integers(len(seeds)))]["level"]
        name = NEW_NAMES[int(r.integers(len(NEW_NAMES)))]
        next_c, next_s = next_c + 1, next_s + 1
        running.append(Campaign(platform, f"{pc}{next_c}", f"{name}_{launch:%Y_%m}", f"{ps}{next_s}", f"{name}_Audience",
                                launch, level={**template, "imp": template["imp"] * float(r.lognormal(0, 0.25))}))
        launch += timedelta(days=LAUNCH_EVERY_DAYS)
    return tuple(paused + running)


def season(d: date) -> float:
    """Demand index: GA day-of-year shape × damped weekday × retail event."""
    cal = _seasonality()
    idx = cal["doy"][d.timetuple().tm_yday - 1] * cal["dow"][d.weekday()] ** DOW_STRENGTH
    for name, days in retail_events(d.year).items():
        if d in days:
            idx *= cal["events"][name]
    return idx


def _q4_cpm(d: date) -> float:
    # ponytail: hand-set Q4 auction inflation (the GA sample has no cost); replace with a measured CPM index if one appears
    if d.month == 11:
        return 1.25
    return 1.35 if d.month == 12 and d.day <= 20 else 1.0


def _plant(campaign_id: str, d: date) -> tuple[str, float] | None:
    r = _rng("plant", campaign_id, d.toordinal())
    if r.random() >= ANOMALY_P:
        return None
    if r.random() < 0.5:
        return "HIGH_CPA", float(r.uniform(0.25, 0.4))    # broken pixel or landing page
    return "LOW_CPA", float(r.uniform(4.0, 6.0))          # duplicate conversion firing


def _drift(c: Campaign, last: date) -> np.ndarray:
    """Multiplicative walk from the campaign's start: ±0.5 %/day, clipped to 0.5–2×. One stream per campaign, so a
    date's value never depends on how far the window reaches."""
    n = (last - c.start).days + 1
    return np.clip(np.exp(np.cumsum(_rng("drift", c.campaign_id).normal(0, 0.005, n))), 0.5, 2.0)


def _facebook(lv, r, row):
    imp = row["impressions"]
    reach = min(imp, round(imp / (lv["freq"] * float(r.lognormal(0, 0.03)))))
    return {"video_views": int(r.binomial(imp, min(lv["vv"], 1.0))),
            "engagement_rate": round(lv["er"] * float(r.lognormal(0, 0.15)), 4),
            "reach": reach, "frequency": round(imp / reach, 2) if reach else 0.0}


def _google(lv, r, row):
    imp, clicks, cost = row["impressions"], row["clicks"], row["cost"]
    return {"conversion_value": round(row["conversions"] * lv["aov"] * float(r.lognormal(0, 0.05)), 2),
            "ctr": round(clicks / imp, 4) if imp else 0.0, "avg_cpc": round(cost / clicks, 2) if clicks else 0.0,
            "quality_score": int(lv["qs"]),
            "search_impression_share": round(min(1.0, lv["sis"] * float(r.lognormal(0, 0.05))), 2)}


def _tiktok(lv, r, row):
    views = [int(r.binomial(row["impressions"], min(lv["vv"], 1.0)))]
    for q in lv["q"]:
        views.append(int(r.binomial(views[-1], min(q, 1.0))))
    return {"video_views": views[0], "video_watch_25": views[1], "video_watch_50": views[2],
            "video_watch_75": views[3], "video_watch_100": views[4],
            "likes": int(r.binomial(views[0], min(lv["like"], 1.0))),
            "shares": int(r.binomial(views[0], min(lv["share"], 1.0))),
            "comments": int(r.binomial(views[0], min(lv["comment"], 1.0)))}


_PLATFORM_COLUMNS = {"facebook": _facebook, "google": _google, "tiktok": _tiktok}


def _row(c: Campaign, d: date, as_of: date, drift: float) -> tuple[dict, tuple[str, float] | None]:
    """One campaign-day as reported on as_of, and the anomaly that took effect in it (None if none did). Every draw
    happens in the same order whatever as_of is: restatement changes counts, never the noise."""
    p, lv = PLATFORMS[c.platform], c.level
    r = _rng("day", c.campaign_id, d.toordinal())
    idx = season(d)
    fatigue = 1 - 0.2 * ((d - c.start).days % CREATIVE_DAYS) / CREATIVE_DAYS
    impressions = int(r.poisson(lv["imp"] * idx ** 0.5 * drift))
    clicks = int(r.binomial(impressions, min(lv["ctr"] * fatigue, 1.0)))
    final = int(r.binomial(clicks, min(lv["cvr"] * idx ** 0.5, 1.0)))
    spend = round(impressions / 1000 * lv["cpm"] * _q4_cpm(d) * float(r.lognormal(0, 0.05)), 2)
    plant = _plant(c.campaign_id, d) if final >= MIN_PLANT_CONVERSIONS else None
    if plant:
        final = min(clicks, round(final * plant[1]))
    row = {"date": d.isoformat(), "campaign_id": c.campaign_id, "campaign_name": c.campaign_name,
           f"{p['sub']}_id": c.sub_id, f"{p['sub']}_name": c.sub_name, "impressions": impressions, "clicks": clicks,
           p["cost"]: spend, "conversions": int(final * MATURITY.get((as_of - d).days, 1.0))}
    row |= _PLATFORM_COLUMNS[c.platform](lv, r, row)
    return row, plant


def _simulate(start: date, end: date, as_of: date, today: date | None) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    today = today or datetime.now(timezone.utc).date()
    if not HISTORY_START <= start <= end:
        raise ValueError(f"need {HISTORY_START} <= start <= end, got {start} … {end}")
    if end >= today:
        raise ValueError(f"end {end} is not before today {today} (UTC): a day is reported only once it is over")
    if as_of <= end:
        raise ValueError(f"as_of {as_of} must be after end {end}")
    frames, labels = {}, []
    for platform, p in PLATFORMS.items():
        rows = []
        for c in roster(platform, end):
            first, last = max(start, c.start), min(end, c.end or end)
            if last < first:
                continue
            walk = _drift(c, last)
            d = first
            while d <= last:
                if _rng("active", c.campaign_id, d.toordinal()).random() < ACTIVE_P:
                    row, plant = _row(c, d, as_of, float(walk[(d - c.start).days]))
                    rows.append(row)
                    if plant:
                        labels.append({"date": d, "platform": p["label"], "campaign_id": c.campaign_id,
                                       "direction": plant[0], "factor": round(plant[1], 3)})
                d += timedelta(days=1)
        frames[platform] = (pd.DataFrame(rows, columns=_levels()[platform]["header"])
                            .sort_values(["date", "campaign_id"], ignore_index=True))
    return frames, pd.DataFrame(labels, columns=["date", "platform", "campaign_id", "direction", "factor"])


def generate(start: date, end: date, as_of: date, *, today: date | None = None) -> dict[str, pd.DataFrame]:
    """Rows for start … end as the platforms report them on as_of, in each real export's column order.
    Same arguments → same bytes, and a date's values depend only on its age (as_of − date)."""
    return _simulate(start, end, as_of, today)[0]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--levels", action="store_true", help="rebuild calibration/levels.json from data/raw (offline)")
    ap.add_argument("--seasonality", action="store_true", help="rebuild calibration/seasonality.json (BigQuery)")
    args = ap.parse_args()
    if not (args.levels or args.seasonality):
        ap.error("pick --levels and/or --seasonality")
    if args.levels:
        write_levels()
    if args.seasonality:
        print(json.dumps(write_seasonality()["events"]))
