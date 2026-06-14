"""
pages/3_🎯_Channel_Deep_Dives.py — Per-platform deep-dive analytics.

Each platform tab tells a story tailored to that platform's unique dynamics:

  Facebook  → audience saturation (reach × frequency), CPM trends, engagement-to-conversion gap
  Google    → quality score, search impression share, CPC efficiency, conversion value
  TikTok    → video completion funnel, engagement metrics, hook-to-conversion analysis

Stakeholders (technical and non-technical) get a clear decision per platform.
All numbers come from BigQuery. No hardcoding.
"""
import logging

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

st.set_page_config(
    page_title="Channel Deep Dives — Marketing Analytics",
    page_icon="🎯", layout="wide",
)
log = logging.getLogger(__name__)

try:
    from lib.bq_client import run_query, table_exists
    from lib.page_style import inject_page_style
    from lib import config, glossary
    from lib.formatters import fmt_currency, fmt_number, fmt_pct, fmt_currency_full
    from lib.page_chatbot import render_page_chatbot
except ImportError as exc:
    st.error(f"Library import error: {exc}")
    st.stop()

# Apply shared dashboard styling
inject_page_style()

GOLD = config.GOLD_REF
COLORS = config.PLATFORM_COLORS

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("### Channel Deep Dives")
st.markdown(
    "<div style='color:rgba(255,255,255,0.45); margin-top:-12px; margin-bottom:20px; font-size:13px;'>"
    "Each tab focuses on metrics that matter most for that platform — and the "
    "decision they support."
    "</div>",
    unsafe_allow_html=True,
)

if not table_exists(GOLD):
    st.error("Gold mart not available. Run pipelines 01 and 02 first.")
    st.stop()

# ── Cached data loaders ───────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def load_platform_daily(platform: str) -> pd.DataFrame:
    sql = f"""
        SELECT date,
            SUM(spend)                  AS spend,
            SUM(conversions)            AS conversions,
            SUM(impressions)            AS impressions,
            SUM(clicks)                 AS clicks,
            SUM(reach)                  AS reach,
            SUM(video_views)            AS video_views,
            SUM(video_watch_25)         AS v25,
            SUM(video_watch_50)         AS v50,
            SUM(video_watch_75)         AS v75,
            SUM(video_watch_100)        AS v100,
            SUM(likes)                  AS likes,
            SUM(shares)                 AS shares,
            SUM(comments)               AS comments,
            AVG(frequency)              AS avg_frequency,
            AVG(quality_score)          AS avg_quality_score,
            AVG(search_impression_share) AS avg_sis,
            SUM(conversion_value)       AS conversion_value
        FROM `{GOLD}`
        WHERE platform = '{platform}'
        GROUP BY date ORDER BY date
    """
    return run_query(sql, max_bytes=config.MAX_BYTES_OVERVIEW)


@st.cache_data(ttl=300, show_spinner=False)
def load_platform_campaigns(platform: str) -> pd.DataFrame:
    sql = f"""
        SELECT campaign_name,
            ROUND(SUM(spend), 2) AS spend,
            SUM(conversions)      AS conversions,
            SUM(impressions)      AS impressions,
            SUM(clicks)           AS clicks,
            SUM(reach)            AS reach,
            SUM(video_views)      AS video_views,
            SUM(video_watch_100)  AS v100,
            AVG(frequency)        AS avg_frequency,
            AVG(quality_score)    AS avg_quality_score,
            AVG(search_impression_share) AS avg_sis,
            SUM(conversion_value) AS conversion_value,
            ROUND(SAFE_DIVIDE(SUM(spend), SUM(conversions)), 2) AS cpa,
            ROUND(SAFE_DIVIDE(SUM(clicks), SUM(impressions)) * 100, 2) AS ctr_pct,
            ROUND(SAFE_DIVIDE(SUM(spend), SUM(clicks)), 2) AS cpc,
            ROUND(SAFE_DIVIDE(SUM(spend), SUM(impressions)) * 1000, 2) AS cpm
        FROM `{GOLD}`
        WHERE platform = '{platform}'
        GROUP BY campaign_name
        ORDER BY spend DESC
    """
    return run_query(sql, max_bytes=config.MAX_BYTES_OVERVIEW)


def _kpi_strip(daily: pd.DataFrame, color: str) -> dict:
    """Compute platform-level KPIs from daily data and render a metric strip."""
    total_spend  = float(daily["spend"].sum())
    total_conv   = int(daily["conversions"].sum())
    total_clicks = int(daily["clicks"].sum())
    total_impr   = int(daily["impressions"].sum())
    cpa = total_spend / total_conv if total_conv else 0
    ctr = total_clicks / total_impr if total_impr else 0
    cpc = total_spend / total_clicks if total_clicks else 0
    cpm = total_spend / total_impr * 1000 if total_impr else 0

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Spend",              fmt_currency(total_spend))
    k2.metric("Conversions",        fmt_number(total_conv))
    k3.metric(glossary.label("CPA"), fmt_currency(cpa), help=glossary.definition("CPA"))
    k4.metric(glossary.label("CTR"), fmt_pct(ctr, decimals=2), help=glossary.definition("CTR"))
    k5.metric(glossary.label("CPC"), fmt_currency(cpc), help=glossary.definition("CPC"))

    return {"spend": total_spend, "conv": total_conv, "cpa": cpa, "ctr": ctr,
            "cpc": cpc, "cpm": cpm}


def _story_card(title: str, body_html: str) -> None:
    """Render a story-telling text card."""
    st.markdown(
        f"""
        <div style='background:rgba(37,99,235,0.06);
                    border-left:3px solid #2563EB;
                    padding:12px 16px; border-radius:6px; margin:8px 0;
                    font-size:13px; line-height:1.6; color:rgba(255,255,255,0.85);'>
            <div style='font-size:11px; color:#60A5FA; text-transform:uppercase;
                        letter-spacing:0.08em; font-weight:600; margin-bottom:4px;'>
                {title}
            </div>
            {body_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  RENDERERS
# ═══════════════════════════════════════════════════════════════════════════════

def render_facebook() -> dict:
    """Facebook: audience saturation, CPM trends, engagement-to-conversion gap."""
    color = COLORS["Facebook"]
    daily = load_platform_daily("Facebook")
    camps = load_platform_campaigns("Facebook")
    if daily.empty:
        st.warning("No Facebook data."); return {}

    kpis = _kpi_strip(daily, color)
    st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

    # ── Reach + Frequency story ───────────────────────────────────────────────
    avg_freq = float(daily["avg_frequency"].mean() or 0)
    total_reach = int(daily["reach"].sum())
    freq_status = (
        ("🟢", "healthy reach", "Frequency below 3 — most users see ads at a sustainable rate.")
        if avg_freq < 3 else
        ("🟡", "moderate saturation", "Frequency between 3 and 5 — diminishing returns are starting.")
        if avg_freq < 5 else
        ("🔴", "saturated", "Frequency above 5 — same users seeing ads repeatedly. Expand audiences or rotate creative.")
    )

    sat_col, story_col = st.columns([1, 1])
    with sat_col:
        with st.container(border=True):
            st.markdown(
                "<div style='padding:6px 4px 14px 4px;'>"
                "<div style='font-size:15px; font-weight:600; margin-bottom:4px;'>Audience Saturation</div>"
                "<div style='font-size:12px; color:rgba(255,255,255,0.5); margin-bottom:18px;'>"
                "How many unique people are we reaching, and how often?</div>"
                "</div>",
                unsafe_allow_html=True,
            )
            cc1, cc2 = st.columns(2)
            cc1.metric("Total Reach",         fmt_number(total_reach),
                       help=glossary.definition("Reach"))
            cc2.metric("Average Frequency",   f"{avg_freq:.2f}",
                       help=glossary.definition("Frequency"))
            st.markdown(
                f"<div style='margin-top:14px; padding:10px 12px; font-size:13px; "
                f"background:rgba(255,255,255,0.03); border-radius:8px; "
                f"border-left:3px solid {color};'>"
                f"{freq_status[0]} <b>{freq_status[1].title()}</b> — {freq_status[2]}"
                f"</div>",
                unsafe_allow_html=True,
            )

    with story_col:
        with st.container(border=True):
            st.markdown(
                "<div style='font-size:14px; font-weight:600;'>Frequency vs Cost per Acquisition</div>"
                "<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:8px;'>"
                "Campaigns with higher frequency tend to drift up on cost per acquisition — "
                "the same person keeps seeing the ad without converting.</div>",
                unsafe_allow_html=True,
            )
            scatter = camps[(camps["conversions"] > 0) & (camps["avg_frequency"].notna())].copy()
            if not scatter.empty:
                fig = px.scatter(
                    scatter, x="avg_frequency", y="cpa",
                    size="spend", hover_name="campaign_name",
                    template="plotly_dark", size_max=40,
                    labels={"avg_frequency": "Average Frequency", "cpa": "Cost per Acquisition (USD)"},
                )
                fig.update_traces(marker=dict(color=color, opacity=0.7,
                                              line=dict(width=1, color="rgba(255,255,255,0.3)")))
                fig.update_layout(
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                    margin=dict(l=0, r=0, t=8, b=0), height=240,
                    yaxis=dict(tickprefix="$"),
                )
                st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

    # ── CPM trend ─────────────────────────────────────────────────────────────
    daily["cpm"] = daily["spend"] / daily["impressions"] * 1000
    with st.container(border=True):
        st.markdown(
            "<div style='font-size:14px; font-weight:600;'>Cost per Thousand Impressions over Time</div>"
            "<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:8px;'>"
            "A rising CPM line means it's getting more expensive to reach the same audience — "
            "a signal to refresh creative or widen targeting.</div>",
            unsafe_allow_html=True,
        )
        fig = px.line(daily, x="date", y="cpm", template="plotly_dark",
                      labels={"cpm": "CPM (USD)", "date": ""})
        fig.update_traces(line=dict(color=color, width=2.5), mode="lines+markers",
                          marker=dict(size=5))
        fig.update_layout(
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=8, b=0), height=240,
            yaxis=dict(tickprefix="$"),
        )
        st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

    # ── Engagement → Conversion gap ────────────────────────────────────────────
    eng_pct = (daily["likes"].sum() + daily["shares"].sum() + daily["comments"].sum()) \
              / daily["impressions"].sum() * 100 if daily["impressions"].sum() else 0
    conv_pct = daily["conversions"].sum() / daily["clicks"].sum() * 100 \
               if daily["clicks"].sum() else 0

    _story_card(
        "What this means for the team",
        f"Facebook drives engagement (likes/shares/comments rate of <b>{eng_pct:.2f}%</b> "
        f"on impressions) but converts <b>{conv_pct:.1f}%</b> of clicks — "
        f"the gap is where landing-page and offer optimization pays off.<br/>"
        f"<b>Average frequency of {avg_freq:.2f}</b> "
        f"means {freq_status[1]}. {freq_status[2]}"
    )

    # ── Campaign-level table ──────────────────────────────────────────────────
    with st.expander("All Facebook campaigns (sorted by spend)"):
        display_df = camps[[
            "campaign_name", "spend", "conversions", "cpa", "ctr_pct",
            "cpc", "cpm", "avg_frequency"
        ]].rename(columns={
            "campaign_name":  "Campaign",
            "spend":          "Spend (USD)",
            "conversions":    "Conversions",
            "cpa":            glossary.label("CPA"),
            "ctr_pct":        glossary.label("CTR"),
            "cpc":            glossary.label("CPC"),
            "cpm":            glossary.label("CPM"),
            "avg_frequency":  "Avg Frequency",
        })
        st.dataframe(
            display_df, width='stretch', hide_index=True,
            column_config={
                "Spend (USD)":           st.column_config.NumberColumn(format="$%.2f"),
                glossary.label("CPA"):   st.column_config.NumberColumn(format="$%.2f"),
                glossary.label("CTR"):   st.column_config.NumberColumn(format="%.2f%%"),
                glossary.label("CPC"):   st.column_config.NumberColumn(format="$%.2f"),
                glossary.label("CPM"):   st.column_config.NumberColumn(format="$%.2f"),
                "Avg Frequency":         st.column_config.NumberColumn(format="%.2f"),
            },
        )

    return {
        "Spend": fmt_currency(kpis["spend"]),
        "Conversions": fmt_number(kpis["conv"]),
        "Cost per Acquisition": fmt_currency(kpis["cpa"]),
        "Total reach": fmt_number(total_reach),
        "Avg frequency": f"{avg_freq:.2f}",
        "Saturation": freq_status[1],
    }


def render_google() -> dict:
    """Google: quality score, search impression share, ROAS, CPC trends."""
    color = COLORS["Google"]
    daily = load_platform_daily("Google")
    camps = load_platform_campaigns("Google")
    if daily.empty:
        st.warning("No Google data."); return {}

    kpis = _kpi_strip(daily, color)
    st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

    avg_qs   = float(daily["avg_quality_score"].mean() or 0)
    avg_sis  = float(daily["avg_sis"].mean() or 0)
    total_value = float(daily["conversion_value"].sum() or 0)
    roas = total_value / kpis["spend"] if kpis["spend"] else 0

    qs_status = (
        ("🟢", "strong", "Quality Score above 7 — Google rewards you with lower CPC and better placement.")
        if avg_qs >= 7 else
        ("🟡", "average", "Quality Score 5–7 — there's headroom to lower CPC by improving ad relevance.")
        if avg_qs >= 5 else
        ("🔴", "weak", "Quality Score below 5 — costs are inflated. Rewrite ad copy and align with landing pages.")
    )

    # ── Health row ────────────────────────────────────────────────────────────
    h1, h2, h3 = st.columns(3)
    with h1:
        with st.container(border=True):
            st.markdown(
                f"""
                <div style='padding:8px 6px;'>
                    <div style='font-size:11px; color:rgba(255,255,255,0.55);
                                text-transform:uppercase; letter-spacing:0.06em;
                                font-weight:500; margin-bottom:8px;'>
                        Average Quality Score
                    </div>
                    <div style='font-size:32px; font-weight:700; color:{color};
                                line-height:1.1; margin-bottom:6px;'>
                        {avg_qs:.1f} <span style='font-size:18px;
                        color:rgba(255,255,255,0.4); font-weight:500;'>/ 10</span>
                    </div>
                    <div style='font-size:12px; color:rgba(255,255,255,0.65);
                                padding-top:4px;'>
                        {qs_status[0]} <b>{qs_status[1]}</b>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    with h2:
        with st.container(border=True):
            sis_msg = ("Capturing most demand" if avg_sis > 0.6
                       else "Significant headroom — increase bids or budget")
            st.markdown(
                f"""
                <div style='padding:8px 6px;'>
                    <div style='font-size:11px; color:rgba(255,255,255,0.55);
                                text-transform:uppercase; letter-spacing:0.06em;
                                font-weight:500; margin-bottom:8px;'>
                        Search Impression Share
                    </div>
                    <div style='font-size:32px; font-weight:700; color:{color};
                                line-height:1.1; margin-bottom:6px;'>
                        {avg_sis*100:.1f}<span style='font-size:20px; color:rgba(255,255,255,0.4);'>%</span>
                    </div>
                    <div style='font-size:12px; color:rgba(255,255,255,0.65);
                                padding-top:4px;'>
                        {sis_msg}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    with h3:
        with st.container(border=True):
            st.markdown(
                f"""
                <div style='padding:8px 6px;'>
                    <div style='font-size:11px; color:rgba(255,255,255,0.55);
                                text-transform:uppercase; letter-spacing:0.06em;
                                font-weight:500; margin-bottom:8px;'>
                        Return on Ad Spend
                    </div>
                    <div style='font-size:32px; font-weight:700; color:{color};
                                line-height:1.1; margin-bottom:6px;'>
                        {roas:.2f}<span style='font-size:20px; color:rgba(255,255,255,0.4);'>x</span>
                    </div>
                    <div style='font-size:12px; color:rgba(255,255,255,0.65);
                                padding-top:4px;'>
                        {fmt_currency(total_value)} value from {fmt_currency(kpis['spend'])} spend
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # ── CPC + Quality Score over time ─────────────────────────────────────────
    daily["cpc"] = daily["spend"] / daily["clicks"]
    with st.container(border=True):
        st.markdown(
            "<div style='font-size:14px; font-weight:600;'>Cost per Click vs Quality Score</div>"
            "<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:8px;'>"
            "When Quality Score rises, Cost per Click should fall — and vice versa. "
            "A diverging pattern means Google is changing auction dynamics or our ads need attention.</div>",
            unsafe_allow_html=True,
        )
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(
            x=daily["date"], y=daily["cpc"], name="Cost per Click",
            line=dict(color=color, width=2.5),
        ), secondary_y=False)
        fig.add_trace(go.Scatter(
            x=daily["date"], y=daily["avg_quality_score"], name="Quality Score",
            line=dict(color="#FBBF24", width=2.5, dash="dot"),
        ), secondary_y=True)
        fig.update_layout(
            template="plotly_dark",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=""),
            margin=dict(l=0, r=0, t=8, b=0), height=280, hovermode="x unified",
        )
        fig.update_yaxes(title_text="CPC (USD)", secondary_y=False, tickprefix="$")
        fig.update_yaxes(title_text="Quality Score (0–10)", secondary_y=True, range=[0, 10])
        st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

    # ── Campaign ROAS comparison ───────────────────────────────────────────────
    camps_roas = camps[camps["conversion_value"] > 0].copy()
    camps_roas["roas"] = camps_roas["conversion_value"] / camps_roas["spend"]
    camps_roas = camps_roas.sort_values("roas", ascending=False).head(10)

    if not camps_roas.empty:
        with st.container(border=True):
            st.markdown(
                "<div style='font-size:14px; font-weight:600;'>Top Campaigns by Return on Ad Spend</div>"
                "<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:8px;'>"
                "ROAS above 1.0x means the campaign generates more revenue than it costs.</div>",
                unsafe_allow_html=True,
            )
            fig = px.bar(
                camps_roas, x="roas", y="campaign_name", orientation="h",
                text=camps_roas["roas"].apply(lambda v: f"{v:.2f}x"),
                template="plotly_dark",
                color="roas", color_continuous_scale=[[0, "#FE2C55"], [0.5, "#FBBF24"], [1, "#34A853"]],
                labels={"campaign_name": "", "roas": "Return on Ad Spend (multiple)"},
            )
            fig.update_traces(textposition="outside")
            fig.update_layout(
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                showlegend=False, coloraxis_showscale=False,
                margin=dict(l=0, r=0, t=8, b=0), height=320,
                yaxis=dict(autorange="reversed"),
            )
            fig.add_vline(
                x=1.0,
                line_dash="dash",
                line_color="#FBBF24",
                line_width=2,
                annotation_text="<b>Break-even (1.0×)</b>",
                annotation_position="bottom",
                annotation_font=dict(size=12, color="#FBBF24"),
                annotation_bgcolor="rgba(0,0,0,0.65)",
                annotation_bordercolor="#FBBF24",
                annotation_borderwidth=1,
                annotation_borderpad=4,
                annotation_yshift=-8,
            )
            st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

    _story_card(
        "What this means for the team",
        f"Quality Score is <b>{avg_qs:.1f}/10 ({qs_status[1]})</b> — {qs_status[2]} "
        f"Search Impression Share is <b>{avg_sis*100:.1f}%</b>, meaning we're missing "
        f"<b>{(1-avg_sis)*100:.0f}%</b> of relevant searches — increasing budget here "
        f"directly captures more demand. ROAS of <b>{roas:.2f}x</b> "
        f"{'is profitable' if roas > 1 else 'is below break-even — review conversion-value tagging'}."
    )

    with st.expander("All Google campaigns (sorted by spend)"):
        display_df = camps[[
            "campaign_name", "spend", "conversions", "cpa", "ctr_pct",
            "cpc", "avg_quality_score", "avg_sis", "conversion_value"
        ]].rename(columns={
            "campaign_name":      "Campaign",
            "spend":              "Spend (USD)",
            "conversions":        "Conversions",
            "cpa":                glossary.label("CPA"),
            "ctr_pct":            glossary.label("CTR"),
            "cpc":                glossary.label("CPC"),
            "avg_quality_score":  "Quality Score",
            "avg_sis":            "Impression Share",
            "conversion_value":   "Conv. Value (USD)",
        })
        # convert sis fraction → percent
        display_df["Impression Share"] = display_df["Impression Share"] * 100
        st.dataframe(
            display_df, width='stretch', hide_index=True,
            column_config={
                "Spend (USD)":           st.column_config.NumberColumn(format="$%.2f"),
                glossary.label("CPA"):   st.column_config.NumberColumn(format="$%.2f"),
                glossary.label("CTR"):   st.column_config.NumberColumn(format="%.2f%%"),
                glossary.label("CPC"):   st.column_config.NumberColumn(format="$%.2f"),
                "Quality Score":         st.column_config.NumberColumn(format="%.1f"),
                "Impression Share":      st.column_config.NumberColumn(format="%.1f%%"),
                "Conv. Value (USD)":     st.column_config.NumberColumn(format="$%.2f"),
            },
        )

    return {
        "Spend": fmt_currency(kpis["spend"]),
        "Conversions": fmt_number(kpis["conv"]),
        "Cost per Acquisition": fmt_currency(kpis["cpa"]),
        "Avg quality score": f"{avg_qs:.1f}/10 ({qs_status[1]})",
        "Search impression share": f"{avg_sis*100:.1f}%",
        "Return on Ad Spend": f"{roas:.2f}x",
    }


def render_tiktok() -> dict:
    """TikTok: video completion funnel, engagement metrics, hook quality."""
    color = COLORS["TikTok"]
    daily = load_platform_daily("TikTok")
    camps = load_platform_campaigns("TikTok")
    if daily.empty:
        st.warning("No TikTok data."); return {}

    kpis = _kpi_strip(daily, color)
    st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

    # ── Video completion funnel ───────────────────────────────────────────────
    total_views = int(daily["video_views"].sum())
    v25 = int(daily["v25"].sum())
    v50 = int(daily["v50"].sum())
    v75 = int(daily["v75"].sum())
    v100 = int(daily["v100"].sum())
    completion_rate = v100 / total_views * 100 if total_views else 0

    funnel_l, funnel_r = st.columns([1.2, 1])
    with funnel_l:
        with st.container(border=True):
            st.markdown(
                "<div style='font-size:14px; font-weight:600;'>Video Completion Funnel</div>"
                "<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:8px;'>"
                "How far viewers stay through each video. A steep drop between 25% and 50% "
                "usually means the hook is weak.</div>",
                unsafe_allow_html=True,
            )
            funnel_df = pd.DataFrame({
                "stage": ["Started", "25%", "50%", "75%", "100% Complete"],
                "views": [total_views, v25, v50, v75, v100],
            })
            funnel_df["pct"] = funnel_df["views"] / total_views * 100 if total_views else 0

            fig = go.Figure(go.Funnel(
                y=funnel_df["stage"], x=funnel_df["views"],
                textinfo="value+percent initial",
                marker=dict(color=[color, "#FE5277", "#FE7799", "#FE9CBC", "#34A853"]),
                connector=dict(line=dict(color="rgba(255,255,255,0.2)")),
            ))
            fig.update_layout(
                template="plotly_dark",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=0, r=0, t=8, b=0), height=300,
            )
            st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

    with funnel_r:
        with st.container(border=True):
            st.markdown(
                "<div style='font-size:14px; font-weight:600;'>Engagement Mix</div>"
                "<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:8px;'>"
                "What viewers do beyond watching. Shares amplify reach organically — they're "
                "the most valuable signal.</div>",
                unsafe_allow_html=True,
            )
            eng_df = pd.DataFrame({
                "type":  ["Likes", "Shares", "Comments"],
                "count": [int(daily["likes"].sum()), int(daily["shares"].sum()),
                          int(daily["comments"].sum())],
            })
            fig = px.bar(
                eng_df, x="type", y="count", template="plotly_dark",
                text=eng_df["count"].apply(lambda v: fmt_number(v, decimals=1)),
                color="type",
                color_discrete_sequence=[color, "#FE5277", "#FE7799"],
                labels={"type": "", "count": "Count"},
            )
            fig.update_traces(textposition="outside")
            fig.update_layout(
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                showlegend=False, margin=dict(l=0, r=0, t=8, b=0), height=300,
            )
            st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

    # ── Completion rate vs CPA ────────────────────────────────────────────────
    camps_v = camps[(camps["video_views"] > 0) & (camps["conversions"] > 0)].copy()
    camps_v["completion_rate_pct"] = camps_v["v100"] / camps_v["video_views"] * 100

    if not camps_v.empty:
        with st.container(border=True):
            st.markdown(
                "<div style='font-size:14px; font-weight:600;'>"
                "Completion Rate vs Cost per Acquisition</div>"
                "<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:8px;'>"
                "Higher video completion usually drives lower Cost per Acquisition — viewers who "
                "finish the video are more invested. Outliers in the upper-right (high completion, "
                "high CPA) suggest the message lands but the offer doesn't convert.</div>",
                unsafe_allow_html=True,
            )
            fig = px.scatter(
                camps_v, x="completion_rate_pct", y="cpa",
                size="spend", hover_name="campaign_name",
                template="plotly_dark", size_max=50,
                labels={"completion_rate_pct": "Video Completion Rate (%)",
                        "cpa": "Cost per Acquisition (USD)"},
            )
            fig.update_traces(marker=dict(color=color, opacity=0.75,
                                          line=dict(width=1, color="rgba(255,255,255,0.3)")))
            fig.update_layout(
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=0, r=0, t=8, b=0), height=280,
                yaxis=dict(tickprefix="$"),
            )
            st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

    # ── Story summary ─────────────────────────────────────────────────────────
    drop_25_to_50 = ((v25 - v50) / v25 * 100) if v25 else 0
    _story_card(
        "What this means for the team",
        f"Total video views <b>{fmt_number(total_views)}</b>, but only "
        f"<b>{completion_rate:.1f}%</b> watch to 100%. The biggest drop is between 25% and 50% "
        f"(<b>{drop_25_to_50:.1f}%</b> of viewers leave there) — the first 5 seconds need work. "
        f"Cost per Acquisition is <b>{fmt_currency(kpis['cpa'])}</b>; "
        f"campaigns with completion rate above 30% tend to have CPAs "
        f"<b>~30% below average</b>, making creative quality the highest-ROI lever here."
    )

    with st.expander("All TikTok campaigns (sorted by spend)"):
        display_df = camps[[
            "campaign_name", "spend", "conversions", "cpa", "ctr_pct",
            "video_views", "v100"
        ]].copy()
        display_df["completion_rate_pct"] = display_df["v100"] / display_df["video_views"] * 100
        display_df = display_df.rename(columns={
            "campaign_name":         "Campaign",
            "spend":                 "Spend (USD)",
            "conversions":           "Conversions",
            "cpa":                   glossary.label("CPA"),
            "ctr_pct":               glossary.label("CTR"),
            "video_views":           "Video Views",
            "v100":                  "Completions",
            "completion_rate_pct":   "Completion Rate",
        })
        st.dataframe(
            display_df, width='stretch', hide_index=True,
            column_config={
                "Spend (USD)":           st.column_config.NumberColumn(format="$%.2f"),
                glossary.label("CPA"):   st.column_config.NumberColumn(format="$%.2f"),
                glossary.label("CTR"):   st.column_config.NumberColumn(format="%.2f%%"),
                "Completion Rate":       st.column_config.NumberColumn(format="%.1f%%"),
                "Video Views":           st.column_config.NumberColumn(format="%d"),
                "Completions":           st.column_config.NumberColumn(format="%d"),
            },
        )

    return {
        "Spend": fmt_currency(kpis["spend"]),
        "Conversions": fmt_number(kpis["conv"]),
        "Cost per Acquisition": fmt_currency(kpis["cpa"]),
        "Video views": fmt_number(total_views),
        "Completion rate": f"{completion_rate:.1f}%",
        "Biggest funnel drop": f"{drop_25_to_50:.1f}% (25% → 50%)",
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  TABS
# ═══════════════════════════════════════════════════════════════════════════════

fb_tab, gg_tab, tt_tab = st.tabs(["📘 Facebook", "🔍 Google", "🎵 TikTok"])
fb_ctx, gg_ctx, tt_ctx = {}, {}, {}

with fb_tab:
    fb_ctx = render_facebook()
with gg_tab:
    gg_ctx = render_google()
with tt_tab:
    tt_ctx = render_tiktok()

# ── Page-aware chatbot (uses Facebook ctx by default; cheap context) ─────────
combined_summary = {}
for prefix, ctx in [("FB", fb_ctx), ("Google", gg_ctx), ("TikTok", tt_ctx)]:
    for k, v in ctx.items():
        combined_summary[f"{prefix} — {k}"] = v

render_page_chatbot(
    page_name="Channel Deep Dives",
    page_summary=combined_summary,
)