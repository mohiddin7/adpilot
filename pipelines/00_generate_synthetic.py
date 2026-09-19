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
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

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
