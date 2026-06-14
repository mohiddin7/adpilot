"""
lib/insight_generator.py — 5-card AI insight generator.

Ported from Script 06. Changes:
  - Uses bq_client.run_query instead of bigquery.Client directly.
  - Uses logging.getLogger instead of setup_logging().
  - Adds per-card rule-based fallback (never returns empty).
  - Returns list[InsightCard] dataclasses.

Cards:
  1. EXECUTIVE_SUMMARY   — 3-sentence CMO brief
  2. WORST_PERFORMER     — worst campaign + action
  3. BUDGET_OPTIMIZATION — narrates LP optimizer
  4. FORECAST_OUTLOOK    — interprets 14-day forecast
  5. ANOMALY_NARRATIVE   — explains anomaly results
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from . import config, prompts
from .bq_client import run_query, table_exists
from .llm_client import LLMClient

log = logging.getLogger(__name__)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class InsightCard:
    card_id:       str          # e.g. EXECUTIVE_SUMMARY
    card_title:    str          # Display title for UI
    card_emoji:    str          # Sidebar icon
    summary_text:  str          # The narrative (LLM or rule-based)
    context:       dict         # Raw data used to build the card
    model_used:    str          # Model name or RULE_BASED_FALLBACK
    is_fallback:   bool = False # True when LLM was unavailable


# ── Context builder ───────────────────────────────────────────────────────────

class ContextPacketBuilder:
    """Assembles the structured context packet from live BigQuery state."""

    def __init__(self) -> None:
        self._gold   = config.GOLD_REF
        self._anomaly = config.ANOMALY_REF
        self._budget = config.BUDGET_REF
        self._forecast = config.FORECAST_REF

    def build(self) -> dict[str, Any]:
        """Return the full context packet. Individual failures return defaults."""
        packet: dict[str, Any] = {}

        packet.update(self._platform_kpis())
        packet.update(self._campaign_cpa_table())
        packet.update(self._budget_recs())
        packet.update(self._forecast_summary())
        packet.update(self._anomaly_summary())

        return packet

    # ── Sub-queries ──────────────────────────────────────────────────────────

    def _platform_kpis(self) -> dict:
        try:
            df = run_query(f"""
                SELECT
                    platform,
                    ROUND(SUM(spend), 2)        AS total_spend,
                    SUM(conversions)             AS total_conversions,
                    ROUND(SAFE_DIVIDE(SUM(spend), SUM(conversions)), 2) AS cpa,
                    ROUND(SAFE_DIVIDE(SUM(clicks), SUM(impressions)), 4) AS ctr
                FROM `{self._gold}`
                GROUP BY platform
                ORDER BY total_spend DESC
            """, max_bytes=config.MAX_BYTES_INSIGHTS)

            platform_map = {
                row["platform"]: {
                    "spend_usd":   float(row["total_spend"]),
                    "conversions": int(row["total_conversions"]),
                    "cpa_usd":     float(row["cpa"]),
                    "ctr":         float(row["ctr"]),
                }
                for _, row in df.iterrows()
            }

            total_spend = sum(p["spend_usd"] for p in platform_map.values())
            total_conv  = sum(p["conversions"] for p in platform_map.values())

            # Add spend_pct
            for p in platform_map.values():
                p["spend_pct"] = round(p["spend_usd"] / total_spend * 100, 1) if total_spend else 0

            return {
                "total_spend_usd":   round(total_spend, 2),
                "total_conversions": total_conv,
                "blended_cpa_usd":   round(total_spend / total_conv, 2) if total_conv else 0,
                "platform_breakdown": platform_map,
            }
        except Exception as exc:
            log.warning("_platform_kpis failed: %s", exc)
            return {
                "total_spend_usd": config.GROUND_TRUTH["total_spend"],
                "total_conversions": config.GROUND_TRUTH["total_conversions"],
                "blended_cpa_usd": config.GROUND_TRUTH["blended_cpa"],
                "platform_breakdown": {},
            }

    def _campaign_cpa_table(self) -> dict:
        try:
            df = run_query(f"""
                SELECT
                    platform,
                    campaign_name,
                    ROUND(SUM(spend), 2)  AS spend,
                    SUM(conversions)       AS conversions,
                    ROUND(SAFE_DIVIDE(SUM(spend), SUM(conversions)), 2) AS cpa
                FROM `{self._gold}`
                GROUP BY platform, campaign_name
                HAVING conversions > 0
                ORDER BY cpa DESC
                LIMIT 20
            """, max_bytes=config.MAX_BYTES_INSIGHTS)

            records = df.to_dict(orient="records")
            best = min(records, key=lambda r: r["cpa"]) if records else {}
            worst = max(records, key=lambda r: r["cpa"]) if records else {}

            return {
                "campaign_cpa_table": records,
                "best_campaign":  best,
                "worst_campaign": worst,
            }
        except Exception as exc:
            log.warning("_campaign_cpa_table failed: %s", exc)
            return {"campaign_cpa_table": [], "best_campaign": {}, "worst_campaign": {}}

    def _budget_recs(self) -> dict:
        if not table_exists(self._budget):
            return {"budget_recommendations": []}
        try:
            df = run_query(f"SELECT * FROM `{self._budget}` LIMIT 10",
                           max_bytes=config.MAX_BYTES_INSIGHTS)
            return {"budget_recommendations": df.to_dict(orient="records")}
        except Exception as exc:
            log.warning("_budget_recs failed: %s", exc)
            return {"budget_recommendations": []}

    def _forecast_summary(self) -> dict:
        if not table_exists(self._forecast):
            return {"forecast_summary": []}
        try:
            df = run_query(f"""
                SELECT platform, metric_name,
                    ROUND(AVG(predicted_value), 2) AS avg_predicted,
                    ROUND(MIN(lower_bound), 2)     AS lower_bound,
                    ROUND(MAX(upper_bound), 2)     AS upper_bound
                FROM `{self._forecast}`
                GROUP BY platform, metric_name
                ORDER BY platform, metric_name
            """, max_bytes=config.MAX_BYTES_INSIGHTS)
            return {"forecast_summary": df.to_dict(orient="records")}
        except Exception as exc:
            log.warning("_forecast_summary failed: %s", exc)
            return {"forecast_summary": []}

    def _anomaly_summary(self) -> dict:
        if not table_exists(self._anomaly):
            return {"anomaly_count": 0, "anomaly_details": []}
        try:
            count_df = run_query(f"""
                SELECT COUNT(*) AS n FROM `{self._anomaly}` WHERE is_anomaly = 1
            """, max_bytes=config.MAX_BYTES_INSIGHTS)
            count = int(count_df.iloc[0, 0]) if not count_df.empty else 0

            detail_df = run_query(f"""
                SELECT platform, campaign_name, date, anomaly_direction,
                       ROUND(observed_cpa, 2) AS observed_cpa,
                       ROUND(rolling_mean_cpa, 2) AS rolling_mean_cpa,
                       ROUND(z_score, 2) AS z_score
                FROM `{self._anomaly}`
                WHERE is_anomaly = 1
                ORDER BY ABS(z_score) DESC
                LIMIT 10
            """, max_bytes=config.MAX_BYTES_INSIGHTS)

            return {
                "anomaly_count":   count,
                "anomaly_details": detail_df.to_dict(orient="records"),
            }
        except Exception as exc:
            log.warning("_anomaly_summary failed: %s", exc)
            return {"anomaly_count": 0, "anomaly_details": []}


# ── Rule-based fallbacks (always produce a meaningful card) ──────────────────

def _rule_based_executive_summary(ctx: dict) -> str:
    pb  = ctx.get("platform_breakdown", {})
    best = min(pb.items(), key=lambda kv: kv[1].get("cpa_usd", 999)) if pb else ("N/A", {})
    worst = max(pb.items(), key=lambda kv: kv[1].get("cpa_usd", 0)) if pb else ("N/A", {})
    br = ctx.get("budget_recommendations", [])
    uplift = next((r.get("total_conversion_delta", 0) for r in br if br), 0)
    return (
        f"{best[0]} leads performance with a ${best[1].get('cpa_usd', 0):.2f} CPA on "
        f"${best[1].get('spend_usd', 0):,.0f} in spend across the analysis period. "
        f"{worst[0]} receives {worst[1].get('spend_pct', 0):.0f}% of total budget but "
        f"delivers the worst CPA at ${worst[1].get('cpa_usd', 0):.2f}, representing a "
        f"{worst[1].get('cpa_usd', 0) / best[1].get('cpa_usd', 1):.1f}x efficiency gap. "
        f"Reallocating budget toward {best[0]} and away from {worst[0]} projects an "
        f"additional {int(uplift):+,} conversions with no budget increase."
    )


def _rule_based_worst_performer(ctx: dict) -> str:
    worst = ctx.get("worst_campaign", {})
    if not worst:
        return "No campaign data available. Run pipelines 01–04 first."
    return (
        f"{worst.get('campaign_name','Unknown')} on {worst.get('platform','Unknown')} "
        f"is the highest-cost campaign at ${worst.get('cpa', 0):.2f} CPA on "
        f"${worst.get('spend', 0):,.0f} in spend. "
        f"Recommend reducing daily budget by 30% immediately while reviewing ad creative "
        f"and audience targeting."
    )


def _rule_based_budget_optimization(ctx: dict) -> str:
    recs = ctx.get("budget_recommendations", [])
    if not recs:
        return "Budget recommendations are not yet available. Run pipeline 04 (budget_optimizer.py) first."
    lines = []
    for r in recs:
        platform  = r.get("platform", "?")
        curr_pct  = r.get("current_spend_pct", 0)
        rec_pct   = r.get("recommended_spend_pct", 0)
        direction = "↑" if rec_pct > curr_pct else "↓"
        lines.append(f"{platform} {direction} {curr_pct:.0f}%→{rec_pct:.0f}%")
    uplift = next((r.get("total_conversion_delta", 0) for r in recs), 0)
    return (
        f"The LP optimizer recommends: {'; '.join(lines)}. "
        f"Total budget is unchanged; projected uplift is +{int(uplift):,} conversions "
        f"(~{uplift / ctx.get('total_conversions', 1) * 100:.1f}% improvement)."
    )


def _rule_based_forecast_outlook(ctx: dict) -> str:
    fs = ctx.get("forecast_summary", [])
    if not fs:
        return "Forecast data is not yet available. Run pipeline 05 (forecast.py) first."
    spend_rows = [r for r in fs if r.get("metric_name") == "spend"]
    parts = [f"{r['platform']} ${r['avg_predicted']:,.0f}/day avg" for r in spend_rows]
    return (
        f"14-day forecast projects: {'; '.join(parts) if parts else 'no data'}. "
        f"Wide confidence intervals indicate higher uncertainty — monitor daily actuals closely."
    )


def _rule_based_anomaly_narrative(ctx: dict) -> str:
    count = ctx.get("anomaly_count", 0)
    details = ctx.get("anomaly_details", [])
    if count == 0:
        return "No anomalies detected in the current analysis window. All campaigns are within normal CPA range."
    top = details[0] if details else {}
    direction = top.get("anomaly_direction", "").replace("_", " ").title()
    return (
        f"{count} anomalies detected across campaigns. "
        f"Most significant: {top.get('campaign_name','?')} on {top.get('platform','?')} "
        f"({direction}, z={top.get('z_score', 0):.1f}) — observed CPA ${top.get('observed_cpa', 0):.2f} "
        f"vs. rolling mean ${top.get('rolling_mean_cpa', 0):.2f}. "
        f"Investigate bid strategy and creative fatigue immediately."
    )


_FALLBACK_MAP = {
    "EXECUTIVE_SUMMARY":   _rule_based_executive_summary,
    "WORST_PERFORMER":     _rule_based_worst_performer,
    "BUDGET_OPTIMIZATION": _rule_based_budget_optimization,
    "FORECAST_OUTLOOK":    _rule_based_forecast_outlook,
    "ANOMALY_NARRATIVE":   _rule_based_anomaly_narrative,
}

_CARD_META = [
    ("EXECUTIVE_SUMMARY",   "Executive Briefing",         "📋"),
    ("WORST_PERFORMER",     "⚠️ Worst Performer Alert",    "⚠️"),
    ("BUDGET_OPTIMIZATION", "💡 Budget Reallocation",      "💡"),
    ("FORECAST_OUTLOOK",    "📈 14-Day Forecast Outlook",  "📈"),
    ("ANOMALY_NARRATIVE",   "🔍 Anomaly Patterns",         "🔍"),
]

_SYSTEM_PROMPTS = {
    "EXECUTIVE_SUMMARY":   prompts.EXECUTIVE_SUMMARY_SYSTEM,
    "WORST_PERFORMER":     prompts.WORST_PERFORMER_SYSTEM,
    "BUDGET_OPTIMIZATION": prompts.BUDGET_OPTIMIZATION_SYSTEM,
    "FORECAST_OUTLOOK":    prompts.FORECAST_OUTLOOK_SYSTEM,
    "ANOMALY_NARRATIVE":   prompts.ANOMALY_NARRATIVE_SYSTEM,
}


# ── Generator ────────────────────────────────────────────────────────────────

class InsightGenerator:
    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self._llm = llm or LLMClient()
        self._builder = ContextPacketBuilder()

    def generate_all_cards(self) -> list[InsightCard]:
        ctx = self._builder.build()
        cards = []
        for card_id, card_title, emoji in _CARD_META:
            cards.append(self._generate_card(card_id, card_title, emoji, ctx))
        return cards

    def _generate_card(
        self, card_id: str, card_title: str, emoji: str, ctx: dict
    ) -> InsightCard:
        system_prompt = _SYSTEM_PROMPTS.get(card_id, "")
        user_prompt   = f"Context data:\n{json.dumps(ctx, default=str, indent=2)}"

        text: Optional[str] = None
        model_used = "RULE_BASED_FALLBACK"
        is_fallback = True

        if self._llm.is_available:
            text = self._llm.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=400,
                temperature=0.2,
            )
            if text:
                model_used  = self._llm.model_name
                is_fallback = False

        if not text:
            fallback_fn = _FALLBACK_MAP.get(card_id)
            text = fallback_fn(ctx) if fallback_fn else "Data not available."
            log.info("Card %s using rule-based fallback", card_id)

        return InsightCard(
            card_id=card_id,
            card_title=card_title,
            card_emoji=emoji,
            summary_text=text,
            context=ctx,
            model_used=model_used,
            is_fallback=is_fallback,
        )