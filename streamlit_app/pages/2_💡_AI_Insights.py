"""
pages/2_💡_AI_Insights.py — AI-generated insight cards with visualizations.

For each card:
  - LLM-generated narrative (or rule-based fallback, clearly labelled)
  - A chart that visualises the underlying data
  - A tabular "source data" expander (NOT JSON)

Five cards:
  1. Executive Briefing      — KPI bars + narrative
  2. Worst Performer Alert   — campaign CPA bar chart + narrative
  3. Budget Reallocation     — current vs recommended bar chart + narrative
  4. 14-Day Forecast Outlook — line chart with confidence bands + narrative
  5. Anomaly Patterns        — anomaly timeline scatter + narrative
"""
import logging

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="AI Insights — Marketing Analytics",
    page_icon="💡", layout="wide",
)
log = logging.getLogger(__name__)

try:
    from lib.insight_generator import InsightGenerator, InsightCard
    from lib.llm_client import LLMClient
    from lib.bq_client import run_query, table_exists
    from lib.page_style import inject_page_style
    from lib import config, glossary
    from lib.formatters import fmt_currency, fmt_number, fmt_pct, fmt_delta
    from lib.page_chatbot import render_page_chatbot
except ImportError as exc:
    st.error(f"Library import error: {exc}")
    st.stop()

# Apply shared dashboard styling
inject_page_style()

# ── Card metadata ─────────────────────────────────────────────────────────────
_GRADIENTS = {
    "EXECUTIVE_SUMMARY":   "linear-gradient(90deg, #2563EB, #7C3AED)",
    "WORST_PERFORMER":     "linear-gradient(90deg, #DC2626, #EA580C)",
    "BUDGET_OPTIMIZATION": "linear-gradient(90deg, #059669, #0891B2)",
    "FORECAST_OUTLOOK":    "linear-gradient(90deg, #7C3AED, #2563EB)",
    "ANOMALY_NARRATIVE":   "linear-gradient(90deg, #D97706, #DC2626)",
}

# ── Chart builders (one per card) ─────────────────────────────────────────────

def _chart_executive(ctx: dict) -> "go.Figure | None":
    """Bar chart: CPA per platform, color-coded."""
    pb = ctx.get("platform_breakdown") or {}
    if not pb:
        return None
    rows = [{
        "platform": p, "cpa": v["cpa_usd"],
        "spend_pct": v.get("spend_pct", 0),
        "conversions": v.get("conversions", 0),
    } for p, v in pb.items()]
    df = pd.DataFrame(rows).sort_values("cpa")
    fig = px.bar(
        df, x="platform", y="cpa",
        color="platform",
        color_discrete_map=config.PLATFORM_COLORS,
        text=df["cpa"].apply(lambda v: f"${v:.2f}"),
        template="plotly_dark",
        labels={"cpa": "Cost per Acquisition (USD)", "platform": ""},
    )
    fig.update_traces(textposition="outside", textfont_size=12)
    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False, margin=dict(l=0, r=0, t=8, b=0), height=240,
        yaxis=dict(tickprefix="$", showgrid=True, gridcolor="rgba(255,255,255,0.05)"),
    )
    return fig


def _chart_worst_performer(ctx: dict) -> "go.Figure | None":
    """Horizontal bar of campaign CPAs, sorted worst to best."""
    table = ctx.get("campaign_cpa_table") or []
    if not table:
        return None
    df = pd.DataFrame(table).sort_values("cpa", ascending=False).head(12)
    fig = px.bar(
        df, x="cpa", y="campaign_name", orientation="h",
        color="platform",
        color_discrete_map=config.PLATFORM_COLORS,
        text=df["cpa"].apply(lambda v: f"${v:.2f}"),
        template="plotly_dark",
        labels={"cpa": "Cost per Acquisition (USD)", "campaign_name": ""},
    )
    fig.update_traces(textposition="outside", textfont_size=11)
    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=""),
        margin=dict(l=0, r=0, t=8, b=0), height=300,
        xaxis=dict(tickprefix="$"),
        yaxis=dict(autorange="reversed"),
    )
    return fig


def _chart_budget(ctx: dict) -> "go.Figure | None":
    """
    Diverging bar showing budget shift: cuts (negative, red) and gains
    (positive, green). One bar per platform = $ change recommended.
    Annotation shows projected conversion delta.
    """
    recs = ctx.get("budget_recommendations") or []
    if not recs:
        return None
    df = pd.DataFrame(recs)
    if "current_spend" not in df.columns or "recommended_spend" not in df.columns:
        return None

    df["delta"] = df["recommended_spend"] - df["current_spend"]
    df = df.sort_values("delta")  # cuts first, gains last

    colors = ["#FE2C55" if d < 0 else "#34A853" for d in df["delta"]]
    text_labels = [
        f"{'−' if d < 0 else '+'}${abs(d)/1000:.1f}K"
        for d in df["delta"]
    ]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["delta"], y=df["platform"],
        orientation="h",
        marker=dict(color=colors),
        text=text_labels,
        textposition="outside",
        hovertemplate=(
            "<b>%{y}</b><br>"
            "Current: $%{customdata[0]:,.0f}<br>"
            "Recommended: $%{customdata[1]:,.0f}<br>"
            "Δ Spend: $%{x:,.0f}<br>"
            "Δ Conversions: %{customdata[2]:+,.0f}"
            "<extra></extra>"
        ),
        customdata=df[["current_spend", "recommended_spend",
                        "conversion_delta"]].values,
    ))

    # Annotate projected conversion delta on each bar
    for _, r in df.iterrows():
        fig.add_annotation(
            x=0, y=r["platform"],
            xshift=8 if r["delta"] < 0 else -8,
            text=f"<i>{int(r['conversion_delta']):+,} conv.</i>",
            showarrow=False,
            font=dict(size=10, color="rgba(255,255,255,0.55)"),
            xanchor="left" if r["delta"] < 0 else "right",
        )

    fig.add_vline(x=0, line_color="rgba(255,255,255,0.3)", line_width=1)

    total_delta = int(df["conversion_delta"].sum())
    fig.update_layout(
        template="plotly_dark",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        margin=dict(l=0, r=0, t=24, b=0), height=240,
        xaxis=dict(title="Recommended Spend Change (USD)", tickprefix="$"),
        yaxis=dict(title=""),
        title=dict(
            text=f"<span style='font-size:12px;color:rgba(255,255,255,0.55)'>"
                 f"Net projected impact: <b style='color:#34A853'>{total_delta:+,} conversions</b>"
                 f"</span>",
            x=0.5, xanchor="center", y=0.98,
        ),
    )
    return fig


def _chart_forecast() -> "go.Figure | None":
    """
    Forecast line chart with historical actuals connected to predictions.
    The transition shows where actuals end and forecast begins, with the
    confidence band shaded around the forecast.
    """
    if not table_exists(config.FORECAST_REF):
        return None
    try:
        df_fc = run_query(
            f"""SELECT target_date, platform, metric_name,
                       predicted_value, lower_bound, upper_bound
                FROM `{config.FORECAST_REF}`
                WHERE metric_name = 'spend'
                ORDER BY platform, target_date""",
            max_bytes=config.MAX_BYTES_INSIGHTS,
        )
    except Exception:
        return None
    if df_fc.empty:
        return None

    # Pull recent historical actuals from gold so the chart shows context
    try:
        df_hist = run_query(
            f"""SELECT date AS target_date, platform,
                       SUM(spend) AS predicted_value
                FROM `{config.GOLD_REF}`
                WHERE date >= DATE_SUB(
                    (SELECT MAX(date) FROM `{config.GOLD_REF}`),
                    INTERVAL 30 DAY)
                GROUP BY date, platform
                ORDER BY platform, date""",
            max_bytes=config.MAX_BYTES_INSIGHTS,
        )
    except Exception:
        df_hist = None

    fig = go.Figure()
    forecast_start = pd.to_datetime(df_fc["target_date"].min())

    for platform, grp_fc in df_fc.groupby("platform"):
        color = config.PLATFORM_COLORS.get(platform, "#888")

        # Historical actuals
        if df_hist is not None and not df_hist.empty:
            grp_hist = df_hist[df_hist["platform"] == platform]
            if not grp_hist.empty:
                fig.add_trace(go.Scatter(
                    x=grp_hist["target_date"], y=grp_hist["predicted_value"],
                    mode="lines", name=f"{platform}",
                    line=dict(color=color, width=2),
                    showlegend=True, legendgroup=platform,
                    hovertemplate=f"<b>{platform}</b><br>Actual: $%{{y:,.0f}}<extra></extra>",
                ))

        # Forecast confidence band
        fig.add_trace(go.Scatter(
            x=grp_fc["target_date"].tolist() + grp_fc["target_date"].tolist()[::-1],
            y=grp_fc["upper_bound"].tolist() + grp_fc["lower_bound"].tolist()[::-1],
            fill="toself", fillcolor=_rgba(color, 0.15),
            line=dict(color="rgba(0,0,0,0)"),
            hoverinfo="skip", showlegend=False, legendgroup=platform,
        ))
        # Forecast central line
        fig.add_trace(go.Scatter(
            x=grp_fc["target_date"], y=grp_fc["predicted_value"],
            mode="lines+markers",
            name=f"{platform} forecast",
            line=dict(color=color, width=2.5, dash="dash"),
            marker=dict(size=4),
            showlegend=False, legendgroup=platform,
            hovertemplate=f"<b>{platform}</b><br>Forecast: $%{{y:,.0f}}<extra></extra>",
        ))

    # Vertical line marking forecast start
    fig.add_vline(
        x=forecast_start, line_dash="dot",
        line_color="rgba(255,255,255,0.35)", line_width=1.5,
        annotation_text="forecast →",
        annotation_position="top",
        annotation_font_color="rgba(255,255,255,0.5)",
        annotation_font_size=10,
    )

    fig.update_layout(
        template="plotly_dark",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=""),
        margin=dict(l=0, r=0, t=8, b=0), height=300,
        yaxis=dict(title="Daily Spend (USD)", tickprefix="$",
                   gridcolor="rgba(255,255,255,0.05)"),
        xaxis=dict(title="", gridcolor="rgba(255,255,255,0.05)"),
        hovermode="x unified",
    )
    return fig


def _chart_anomaly() -> "go.Figure | None":
    """
    Story-telling anomaly chart with statistical honesty:

    - Observed Cost per Acquisition vs 7-day rolling baseline per platform.
    - Only days AFTER the warm-up window (first 7 days) get anomaly markers.
      Rolling statistics computed on < 7 days of history are not meaningful.
    - Display threshold: z >= 2.5 (more conservative than the script's 2.0).
    - Warm-up window is visually shaded so users see why early days have no markers.
    """
    if not table_exists(config.ANOMALY_REF):
        return None
    try:
        df = run_query(
            f"""
            WITH bounds AS (
              SELECT MIN(date) AS min_d FROM `{config.ANOMALY_REF}`
            )
            SELECT
                a.date,
                a.platform,
                AVG(a.observed_cpa)     AS observed_cpa,
                AVG(a.rolling_mean_cpa) AS rolling_mean,
                MAX(ABS(a.z_score))     AS max_abs_z,
                -- Anomaly only counts if it's outside the 7-day warm-up window
                MAX(IF(a.is_anomaly = 1
                       AND ABS(a.z_score) >= 2.5
                       AND a.date >= DATE_ADD(b.min_d, INTERVAL 7 DAY),
                       1, 0)) AS is_severe
            FROM `{config.ANOMALY_REF}` a
            CROSS JOIN bounds b
            GROUP BY a.date, a.platform
            ORDER BY a.date
            """,
            max_bytes=config.MAX_BYTES_INSIGHTS,
        )
    except Exception:
        return None
    if df.empty:
        return None

    # Determine warm-up region bounds for the shaded rectangle
    min_date  = pd.to_datetime(df["date"].min())
    warmup_end = min_date + pd.Timedelta(days=7)

    fig = go.Figure()

    # Warm-up shaded region (first 7 days where rolling stats are unreliable)
    fig.add_vrect(
        x0=min_date, x1=warmup_end,
        fillcolor="rgba(245,158,11,0.08)",
        line_width=0,
        annotation_text="warm-up · no anomaly detection",
        annotation_position="top left",
        annotation_font=dict(size=10, color="rgba(245,158,11,0.85)"),
    )

    for platform, grp in df.groupby("platform"):
        color = config.PLATFORM_COLORS.get(platform, "#888")

        # Baseline (rolling mean) — thinner, dotted
        fig.add_trace(go.Scatter(
            x=grp["date"], y=grp["rolling_mean"],
            mode="lines", name=f"{platform} baseline",
            line=dict(color=color, width=1, dash="dot"),
            opacity=0.45,
            hovertemplate="Baseline: $%{y:.2f}<extra></extra>",
            showlegend=False,
        ))
        # Observed CPA
        fig.add_trace(go.Scatter(
            x=grp["date"], y=grp["observed_cpa"],
            mode="lines+markers", name=platform,
            line=dict(color=color, width=2),
            marker=dict(size=4),
            hovertemplate="<b>%{fullData.name}</b><br>Observed: $%{y:.2f}<extra></extra>",
        ))

        # Severe anomaly markers ONLY (z >= 2.5 AND past warm-up)
        severe = grp[grp["is_severe"] == 1]
        if not severe.empty:
            fig.add_trace(go.Scatter(
                x=severe["date"], y=severe["observed_cpa"],
                mode="markers", name=f"{platform} severe",
                marker=dict(size=14, color=color, symbol="circle-open",
                            line=dict(width=3, color="white")),
                showlegend=False,
                hovertemplate=("<b>SEVERE ANOMALY</b><br>"
                                "Cost per Acquisition: $%{y:.2f}<br>"
                                "Z-score: %{customdata:.2f}<extra></extra>"),
                customdata=severe["max_abs_z"],
            ))

    fig.update_layout(
        template="plotly_dark",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=""),
        margin=dict(l=0, r=0, t=8, b=0), height=320,
        yaxis=dict(title="Cost per Acquisition (USD)", tickprefix="$",
                   gridcolor="rgba(255,255,255,0.05)"),
        xaxis=dict(title="", gridcolor="rgba(255,255,255,0.05)"),
        hovermode="x unified",
    )
    return fig


def _rgba(hex_color: str, alpha: float) -> str:
    """Convert #RRGGBB → rgba(r,g,b,a)."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


# ── Source-data table builders (tabular instead of JSON) ─────────────────────

def _table_executive(ctx: dict) -> pd.DataFrame:
    pb = ctx.get("platform_breakdown") or {}
    if not pb:
        return pd.DataFrame()
    rows = [{
        "Platform": p,
        "Spend (USD)": v.get("spend_usd"),
        "Spend %": v.get("spend_pct"),
        "Conversions": v.get("conversions"),
        glossary.label("CPA"): v.get("cpa_usd"),
        glossary.label("CTR"): v.get("ctr", 0) * 100 if v.get("ctr") is not None else None,
    } for p, v in pb.items()]
    return pd.DataFrame(rows).sort_values("Spend (USD)", ascending=False)


def _table_worst(ctx: dict) -> pd.DataFrame:
    rows = ctx.get("campaign_cpa_table") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "platform": "Platform", "campaign_name": "Campaign",
        "spend": "Spend (USD)", "conversions": "Conversions",
        "cpa": glossary.label("CPA"),
    })
    return df.sort_values(glossary.label("CPA"), ascending=False)


def _table_budget(ctx: dict) -> pd.DataFrame:
    rows = ctx.get("budget_recommendations") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    keep = [c for c in ["platform", "current_spend", "current_spend_pct",
                         "recommended_spend", "recommended_spend_pct",
                         "projected_conversions", "conversion_delta"] if c in df.columns]
    return df[keep].rename(columns={
        "platform": "Platform",
        "current_spend": "Current Spend",
        "current_spend_pct": "Current %",
        "recommended_spend": "Recommended Spend",
        "recommended_spend_pct": "Recommended %",
        "projected_conversions": "Projected Conv.",
        "conversion_delta": "Δ Conv.",
    })


def _table_forecast(ctx: dict) -> pd.DataFrame:
    rows = ctx.get("forecast_summary") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "platform": "Platform", "metric_name": "Metric",
        "avg_predicted": "Avg Forecast",
        "lower_bound": "Lower (80% CI)",
        "upper_bound": "Upper (80% CI)",
    })
    return df


def _table_anomaly(ctx: dict) -> pd.DataFrame:
    rows = ctx.get("anomaly_details") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "platform": "Platform", "campaign_name": "Campaign",
        "date": "Date", "observed_cpa": "Observed CPA",
        "rolling_mean_cpa": "Rolling Mean CPA",
        "z_score": "Z-Score", "anomaly_direction": "Direction",
    })
    return df


_CARD_RENDERERS = {
    "EXECUTIVE_SUMMARY":   (_chart_executive, _table_executive),
    "WORST_PERFORMER":     (_chart_worst_performer, _table_worst),
    "BUDGET_OPTIMIZATION": (_chart_budget, _table_budget),
    "FORECAST_OUTLOOK":    (lambda ctx: _chart_forecast(), _table_forecast),
    "ANOMALY_NARRATIVE":   (lambda ctx: _chart_anomaly(),  _table_anomaly),
}


def render_card(card: InsightCard) -> None:
    gradient = _GRADIENTS.get(card.card_id, "linear-gradient(90deg, #2563EB, #7C3AED)")

    # Clear AI vs rule-based labelling
    if card.is_fallback:
        source_badge = (
            "<span style='background:rgba(245,158,11,0.12); color:#F59E0B; "
            "font-size:10px; padding:2px 8px; border-radius:10px; margin-left:8px; "
            "font-weight:600; letter-spacing:0.04em;'>RULE-BASED</span>"
        )
    else:
        source_badge = (
            f"<span style='background:rgba(37,99,235,0.12); color:#60A5FA; "
            f"font-size:10px; padding:2px 8px; border-radius:10px; margin-left:8px; "
            f"font-weight:600; letter-spacing:0.04em;'>AI · {card.model_used}</span>"
        )

    with st.container(border=True):
        st.markdown(
            f"""
            <div style='height:3px; background:{gradient}; border-radius:3px 3px 0 0;
                        margin:-1px -1px 14px -1px;'></div>
            <div style='font-size:18px; font-weight:700; margin-bottom:2px;'>
                {card.card_title} {source_badge}
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Narrative
        st.markdown(
            f"<div style='font-size:14px; line-height:1.7; color:rgba(255,255,255,0.85); "
            f"margin-bottom:14px;'>{card.summary_text}</div>",
            unsafe_allow_html=True,
        )

        # Chart
        chart_builder, table_builder = _CARD_RENDERERS.get(card.card_id, (None, None))
        if chart_builder:
            try:
                fig = chart_builder(card.context)
                if fig is not None:
                    st.plotly_chart(fig, width='stretch',
                                    config={"displayModeBar": False})
            except Exception as exc:
                log.warning("Chart render failed for %s: %s", card.card_id, exc)

        # Tabular source data (NOT JSON)
        if table_builder:
            try:
                df = table_builder(card.context)
                if not df.empty:
                    with st.expander("View source data"):
                        st.dataframe(
                            df, width='stretch', hide_index=True,
                            column_config=_column_config_for(df),
                        )
            except Exception as exc:
                log.warning("Table render failed for %s: %s", card.card_id, exc)


def _column_config_for(df: pd.DataFrame) -> dict:
    """Auto-format known columns."""
    cfg = {}
    for col in df.columns:
        lower = col.lower()
        if "spend" in lower or "cpa" in lower or "(usd)" in lower or "rolling mean" in lower or "observed cpa" in lower:
            cfg[col] = st.column_config.NumberColumn(format="$%.2f")
        elif "%" in col or "ctr" in lower:
            cfg[col] = st.column_config.NumberColumn(format="%.1f%%")
        elif "z-score" in lower or "z_score" in lower:
            cfg[col] = st.column_config.NumberColumn(format="%.2f")
        elif "conv" in lower:
            cfg[col] = st.column_config.NumberColumn(format="%d")
    return cfg


# ── Cached card loader ────────────────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def get_cards() -> list:
    gen = InsightGenerator(llm=LLMClient())
    return gen.generate_all_cards()


# ── Page layout ───────────────────────────────────────────────────────────────
hdr_l, hdr_r = st.columns([4, 1])
with hdr_l:
    st.markdown("### AI Insights")
    st.markdown(
        "<div style='color:rgba(255,255,255,0.45); margin-top:-12px; margin-bottom:24px; font-size:13px;'>"
        "Five perspectives on marketing performance. Each card includes a "
        "data visualization and source table."
        "</div>",
        unsafe_allow_html=True,
    )
with hdr_r:
    st.markdown("<div style='margin-top:38px;'></div>", unsafe_allow_html=True)
    if st.button("↻ Regenerate", width='stretch', type="secondary"):
        get_cards.clear()
        st.rerun()

llm = LLMClient()
if not llm.is_available:
    st.warning(
        "LLM not configured — cards use rule-based fallbacks. Set "
        "`LLM_ENDPOINT_URL`, `LLM_BEARER_TOKEN`, and `LLM_TARGET_MODEL` "
        "to enable AI-generated narratives.",
        icon="⚠️",
    )

with st.spinner("Generating insights from live data…"):
    try:
        cards = get_cards()
    except Exception as exc:
        st.error(f"Failed to generate insights: {exc}")
        log.exception("Insight generation error")
        st.stop()

if not cards:
    st.warning("No cards returned. Confirm pipelines 01–05 have run.")
    st.stop()

# Row 1: Executive briefing (full width)
render_card(cards[0])
st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

# Row 2: Worst performer + Budget reallocation
r1c1, r1c2 = st.columns(2)
with r1c1: render_card(cards[1])
with r1c2: render_card(cards[2])
st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

# Row 3: Forecast + Anomaly
r2c1, r2c2 = st.columns(2)
with r2c1: render_card(cards[3])
with r2c2: render_card(cards[4])

# ── Anomaly algorithm note (honest about limitations) ─────────────────────────
with st.expander("🔬 How are anomalies detected?", expanded=False):
    st.markdown("""
**Algorithm:** Rolling 7-day Z-score per (platform, campaign) on observed Cost per Acquisition.

A day is flagged when:

```
|observed_cpa − rolling_7day_mean| / rolling_7day_std  ≥  threshold
```

**Display threshold on this chart: 2.5** (more conservative). The underlying
`fct_anomaly_flags` table uses threshold 2.0; this chart shows only the severe
deviations to avoid noise.

**Warm-up window: first 7 days are excluded from anomaly detection.**
Rolling statistics computed on fewer than 7 days of history have no statistical
meaning — a single bad value would dominate the "baseline." Days 1-7 still
appear on the chart for visual context (baseline + observed lines) but get no
anomaly markers. The amber-shaded region marks this period.

**Brutally honest limitations of this approach for a 30-day dataset:**

1. After excluding the 7-day warm-up, only 23 days have trustworthy z-scores.
2. A single outlier shifts the rolling mean, which can make the *following*
   day look anomalous too (lag bias).
3. Standard z-score assumes a normal distribution. Cost per Acquisition
   distributions are typically right-skewed.
4. With ~23 days × 3 platforms × 4 campaigns ≈ 276 observations and a 2.5
   threshold (≈ 1.2% expected false positive rate), expect 3-4 false alarms
   from random variation alone.

**Production-grade upgrades for `pipelines/03_anomaly_detection.py`:**

- **Modified Z-score using Median Absolute Deviation (MAD)** — robust to
  outliers, removes the lag bias. Formula:
  `M = 0.6745 × (x − median) / MAD`, flag when `|M| ≥ 3.5`.
- **Minimum-history gate**: don't emit anomaly flags for any row where the
  group has < 7 days of prior observations (enforced in the script, not just
  the display).
- **Day-of-week baseline**: marketing data has weekly cycles. Compare
  Tuesdays to Tuesdays, not to the average of all days.
- **Once you have 60-90 days of data**, the current algorithm will stabilize
  on its own — the limitation is data length, not the method.
    """)

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
fallback_count = sum(1 for c in cards if c.is_fallback)
ai_count = len(cards) - fallback_count
st.markdown(
    f"<div style='font-size:12px; color:rgba(255,255,255,0.3);'>"
    f"{ai_count}/5 AI-generated · {fallback_count}/5 rule-based fallback · "
    f"Cache TTL 10 minutes"
    f"</div>",
    unsafe_allow_html=True,
)

# ── Page-aware chatbot ────────────────────────────────────────────────────────
ctx = cards[0].context if cards else {}
render_page_chatbot(
    page_name="AI Insights",
    page_summary={
        "Total spend":       fmt_currency(ctx.get("total_spend_usd")),
        "Total conversions": fmt_number(ctx.get("total_conversions")),
        "Blended CPA":       fmt_currency(ctx.get("blended_cpa_usd")),
        "Best campaign":     (ctx.get("best_campaign") or {}).get("campaign_name", "—"),
        "Worst campaign":    (ctx.get("worst_campaign") or {}).get("campaign_name", "—"),
        "Anomaly count":     ctx.get("anomaly_count", 0),
        "AI-generated cards":   f"{ai_count}/5",
        "Rule-based fallback":  f"{fallback_count}/5",
    },
)