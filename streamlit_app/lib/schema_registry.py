"""
lib/schema_registry.py — Gold mart column catalogue.

Single source of truth consumed by:
  - sql_agent.py  → embeds into LLM system prompt
  - sql_validator.py → column allowlist
  - pages        → filterable column reference
"""
from __future__ import annotations

GOLD_COLUMNS: dict[str, str] = {
    # ── Dimensions ────────────────────────────────────────────────────────────
    "date":                  "DATE — campaign date; partition key; range 2020-01-01 to present",
    "platform":              "STRING — one of: Facebook | Google | TikTok",
    "campaign_id":           "STRING — platform-native campaign identifier; NOT NULL",
    "campaign_name":         "STRING — human-readable display name",
    "sub_group_id":          "STRING — ad_set_id (Facebook) | ad_group_id (Google) | adgroup_id (TikTok); NOT NULL",
    "sub_group_name":        "STRING — human-readable sub-group name",
    # ── Core metrics ──────────────────────────────────────────────────────────
    "impressions":           "INT64 — total ad impressions served; ≥ 0",
    "clicks":                "INT64 — total ad clicks; ≥ 0; ≤ impressions",
    "spend":                 "FLOAT64 — total cost in USD; ≥ 0",
    "conversions":           "INT64 — total conversion events; ≥ 0; ≤ clicks",
    "conversion_value":      "FLOAT64 — revenue attributed in USD; Google only; 0 for FB and TikTok",
    # ── Video metrics (TikTok-primary; 0 for others) ──────────────────────────
    "video_views":           "INT64 — total video view events",
    "video_watch_25":        "INT64 — users who watched ≥ 25% of video; TikTok only",
    "video_watch_50":        "INT64 — users who watched ≥ 50% of video; TikTok only",
    "video_watch_75":        "INT64 — users who watched ≥ 75% of video; TikTok only",
    "video_watch_100":       "INT64 — users who watched 100% of video; TikTok only",
    # ── Engagement (TikTok-primary; 0 for others) ─────────────────────────────
    "likes":                 "INT64 — post likes; TikTok only",
    "shares":                "INT64 — post shares; TikTok only",
    "comments":              "INT64 — post comments; TikTok only",
    # ── Facebook-specific ─────────────────────────────────────────────────────
    "reach":                 "INT64 — unique users reached; Facebook only; ≤ impressions",
    "frequency":             "FLOAT64 — avg impressions per unique user; Facebook only; ≥ 1.0",
    "engagement_rate":       "FLOAT64 — engagement events ÷ impressions; 0.0–1.0",
    # ── Google-specific ───────────────────────────────────────────────────────
    "quality_score":         "INT64 — Google Ads quality score; 1–10; 0 when not applicable",
    "search_impression_share":"FLOAT64 — share of eligible search impressions captured; Google only; 0.0–1.0",
    "avg_cpc":               "FLOAT64 — average cost-per-click reported by Google; 0 otherwise",
    # ── Computed metrics (all platforms) ──────────────────────────────────────
    "cpa":                   "FLOAT64 — cost per acquisition: SAFE_DIVIDE(spend, conversions)",
    "ctr":                   "FLOAT64 — click-through rate: SAFE_DIVIDE(clicks, impressions)",
    "cpc":                   "FLOAT64 — cost per click: SAFE_DIVIDE(spend, clicks)",
    "cpm":                   "FLOAT64 — cost per thousand impressions: SAFE_DIVIDE(spend, impressions) * 1000",
    "roas":                  "FLOAT64 — return on ad spend: SAFE_DIVIDE(conversion_value, spend); Google only",
    # ── Audit columns ─────────────────────────────────────────────────────────
    "ingested_at":           "TIMESTAMP — UTC timestamp when this row was loaded to bronze",
    "source_file":           "STRING — filename of the CSV that produced this row",
}

# Ordered list for display purposes
COLUMN_NAMES: list[str] = list(GOLD_COLUMNS.keys())

# Total count — 30 business columns + 2 audit columns (ingested_at, source_file)
# The LLD Data Contract says "30 columns" but that refers to business columns.
# The physical table has 32 (30 business + 2 audit).
EXPECTED_COLUMN_COUNT = 32
assert len(GOLD_COLUMNS) == EXPECTED_COLUMN_COUNT, (
    f"schema_registry has {len(GOLD_COLUMNS)} columns, expected {EXPECTED_COLUMN_COUNT}"
)

# Metric columns (numeric — useful for chart selector)
METRIC_COLUMNS: list[str] = [
    "impressions", "clicks", "spend", "conversions", "conversion_value",
    "video_views", "video_watch_25", "video_watch_50", "video_watch_75", "video_watch_100",
    "likes", "shares", "comments", "reach", "frequency", "engagement_rate",
    "quality_score", "search_impression_share", "avg_cpc",
    "cpa", "ctr", "cpc", "cpm", "roas",
]

# Dimension columns
DIMENSION_COLUMNS: list[str] = [
    "date", "platform", "campaign_id", "campaign_name",
    "sub_group_id", "sub_group_name", "ingested_at", "source_file",
]


def schema_for_prompt() -> str:
    """
    Format the schema as a compact string for embedding in LLM system prompts.
    Returns one 'column — description' line per column.
    """
    lines = [f"  {col} — {desc}" for col, desc in GOLD_COLUMNS.items()]
    return "\n".join(lines)