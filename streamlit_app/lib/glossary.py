"""
lib/glossary.py — Full-form metric labels and definitions.

Senior analysts don't show "CPA" — they show "Cost per Acquisition (CPA)" so
both technical and non-technical stakeholders understand. Each metric also has
a one-line definition for help/tooltip use.
"""
from __future__ import annotations

# ── Metric labels: short ↔ full ────────────────────────────────────────────────

METRIC_LABELS: dict[str, str] = {
    "CPA":  "Cost per Acquisition",
    "CTR":  "Click-Through Rate",
    "CPC":  "Cost per Click",
    "CPM":  "Cost per Thousand Impressions",
    "ROAS": "Return on Ad Spend",
}

# ── Metric definitions for tooltips/help text ─────────────────────────────────

METRIC_DEFINITIONS: dict[str, str] = {
    "CPA":  "Total spend divided by total conversions. Lower is better.",
    "CTR":  "Clicks divided by impressions. Measures how often viewers engage.",
    "CPC":  "Total spend divided by total clicks. Lower is better.",
    "CPM":  "Cost per 1,000 impressions. Lower indicates cheaper reach.",
    "ROAS": "Revenue divided by spend. Above 1.0 means the campaign is profitable.",
    "Spend":        "Total amount paid to the ad platform in the selected period.",
    "Conversions":  "Number of qualified outcomes (signups, purchases, etc).",
    "Impressions":  "Number of times an ad was displayed.",
    "Clicks":       "Number of times users clicked on an ad.",
    "Reach":        "Number of unique users who saw an ad. (Facebook only)",
    "Frequency":    "Average number of times each user saw an ad. (Facebook only)",
    "Quality Score":"Google's relevance rating from 1–10. Higher reduces CPC.",
    "Engagement Rate": "Engagement events divided by impressions.",
    "Video Views":  "Number of times the video began playing.",
}


def label(short: str, parenthetical: bool = True) -> str:
    """
    Convert "CPA" → "Cost per Acquisition (CPA)".
    With parenthetical=False, returns just the full name.
    """
    short_clean = short.strip().upper()
    full = METRIC_LABELS.get(short_clean)
    if not full:
        return short
    return f"{full} ({short_clean})" if parenthetical else full


def definition(metric: str) -> str:
    """Return a one-line definition for use in st.help or tooltips."""
    key = metric.strip().upper()
    return METRIC_DEFINITIONS.get(key, METRIC_DEFINITIONS.get(metric.strip(), ""))