"""
pages/1_📊_Performance_Overview.py — Performance Overview.

Senior-analyst layout:
  - Date + platform filters (top)
  - KPI strip with full-form metric names + compact number formatting
  - Daily performance trend (with annotations)
  - Platform comparison table (sortable)
  - Campaign efficiency scatter (the story chart)
  - Best/worst callouts

Channel deep dives have moved to their own page (3_🎯_Channel_Deep_Dives.py).
"""
import logging

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

st.set_page_config(
    page_title="Performance Overview — Marketing Analytics",
    page_icon="📊", layout="wide",
)

log = logging.getLogger(__name__)

try:
    from lib.bq_client import run_query, table_exists
    from lib.page_style import inject_page_style
    from lib import config, glossary
    from lib.formatters import (fmt_currency, fmt_number, fmt_pct, fmt_delta,
                                 fmt_currency_full)
    from lib.page_chatbot import render_page_chatbot
except ImportError as exc:
    st.error(f"Library import error: {exc}")
    st.stop()

# Apply shared dashboard styling
inject_page_style()

PLATFORM_COLORS = config.PLATFORM_COLORS
GOLD = config.GOLD_REF

# ── Data loaders (cached) ─────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def get_available_date_range() -> tuple:
    df = run_query(
        f"SELECT MIN(date) AS start_d, MAX(date) AS end_d FROM `{GOLD}`",
        max_bytes=config.MAX_BYTES_OVERVIEW,
    )
    return pd.to_datetime(df.iloc[0]["start_d"]), pd.to_datetime(df.iloc[0]["end_d"])


@st.cache_data(ttl=300, show_spinner=False)
def load_trend(start_date: str, end_date: str, platforms: tuple) -> pd.DataFrame:
    pl = ", ".join(f"'{p}'" for p in platforms)
    sql = f"""
        SELECT date, platform,
               SUM(spend)       AS spend,
               SUM(conversions) AS conversions,
               SUM(impressions) AS impressions,
               SUM(clicks)      AS clicks
        FROM `{GOLD}`
        WHERE date BETWEEN '{start_date}' AND '{end_date}' AND platform IN ({pl})
        GROUP BY date, platform ORDER BY date, platform
    """
    return run_query(sql, max_bytes=config.MAX_BYTES_OVERVIEW)


@st.cache_data(ttl=300, show_spinner=False)
def load_campaigns(start_date: str, end_date: str, platforms: tuple) -> pd.DataFrame:
    pl = ", ".join(f"'{p}'" for p in platforms)
    sql = f"""
        SELECT platform, campaign_name,
               ROUND(SUM(spend), 2) AS spend,
               SUM(conversions)      AS conversions,
               SUM(impressions)      AS impressions,
               SUM(clicks)           AS clicks,
               ROUND(SAFE_DIVIDE(SUM(spend), SUM(conversions)), 2) AS cpa,
               ROUND(SAFE_DIVIDE(SUM(clicks), SUM(impressions)) * 100, 2) AS ctr_pct
        FROM `{GOLD}`
        WHERE date BETWEEN '{start_date}' AND '{end_date}' AND platform IN ({pl})
        GROUP BY platform, campaign_name
        ORDER BY cpa DESC
    """
    return run_query(sql, max_bytes=config.MAX_BYTES_OVERVIEW)


# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("### Performance Overview")
st.markdown(
    "<div style='color:rgba(255,255,255,0.45); margin-top:-12px; margin-bottom:20px; font-size:13px;'>"
    "Daily marketing performance across all paid channels"
    "</div>",
    unsafe_allow_html=True,
)

if not table_exists(GOLD):
    st.error("Gold mart not available. Run pipelines 01 and 02 first.")
    st.stop()

# ── Filters ─────────────────────────────────────────────────────────────────
try:
    available_start, available_end = get_available_date_range()
except Exception as exc:
    st.error(f"Could not read date range: {exc}")
    st.stop()

with st.container():
    fc1, fc2, fc3 = st.columns([1.5, 1.5, 2])
    with fc1:
        start_date = st.date_input(
            "Start date",
            value=available_start,
            min_value=available_start,
            max_value=available_end,
        ).strftime("%Y-%m-%d")
    with fc2:
        end_date = st.date_input(
            "End date",
            value=available_end,
            min_value=available_start,
            max_value=available_end,
        ).strftime("%Y-%m-%d")
    with fc3:
        platforms_sel = st.multiselect(
            "Platforms",
            options=["Facebook", "Google", "TikTok"],
            default=["Facebook", "Google", "TikTok"],
        )

if not platforms_sel:
    st.warning("Select at least one platform.")
    st.stop()

platforms_tuple = tuple(sorted(platforms_sel))
sql_platforms = ", ".join(f"'{p}'" for p in platforms_sel)
# ── Load data ─────────────────────────────────────────────────────────────────
with st.spinner("Loading performance data…"):
    try:
        df_trend     = load_trend(start_date, end_date, platforms_tuple)
        df_campaigns = load_campaigns(start_date, end_date, platforms_tuple)
    except Exception as exc:
        st.error(f"Query failed: {exc}")
        st.stop()

if df_trend.empty:
    st.warning("No data for the selected filters.")
    st.stop()

# ── KPI strip ────────────────────────────────────────────────────────────────
total_spend  = float(df_trend["spend"].sum())
total_conv   = int(df_trend["conversions"].sum())
total_impr   = int(df_trend["impressions"].sum())
total_clicks = int(df_trend["clicks"].sum())
blended_cpa  = (total_spend / total_conv) if total_conv else 0
blended_ctr  = (total_clicks / total_impr) if total_impr else 0

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Spend",        fmt_currency(total_spend), help=glossary.definition("Spend"))
k2.metric("Total Conversions",  fmt_number(total_conv),    help=glossary.definition("Conversions"))
k3.metric(glossary.label("CPA"), fmt_currency(blended_cpa), help=glossary.definition("CPA"))
k4.metric(glossary.label("CTR"), fmt_pct(blended_ctr, decimals=2), help=glossary.definition("CTR"))

st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

# ── Daily performance trend ───────────────────────────────────────────────────
with st.container(border=True):
    st.markdown(
        "<div style='font-size:14px; font-weight:600; margin-bottom:2px;'>Daily Performance Trend</div>"
        "<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:12px;'>"
        "Tracks daily Spend and Conversions side by side. When the lines diverge — Spend up, "
        "Conversions flat — efficiency is dropping."
        "</div>",
        unsafe_allow_html=True,
    )

    # Two metrics, dual y-axis, color-coded
    df_agg = df_trend.groupby("date")[["spend", "conversions"]].sum().reset_index()

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Scatter(
            x=df_agg["date"], y=df_agg["spend"], name="Spend",
            fill="tozeroy", line=dict(color="#2563EB", width=2),
            fillcolor="rgba(37,99,235,0.15)",
            hovertemplate="Spend: $%{y:,.0f}<extra></extra>",
        ), secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=df_agg["date"], y=df_agg["conversions"], name="Conversions",
            line=dict(color="#34A853", width=2.5),
            mode="lines+markers", marker=dict(size=5),
            hovertemplate="Conversions: %{y:,}<extra></extra>",
        ), secondary_y=True,
    )
    fig.update_layout(
        template="plotly_dark",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=""),
        hovermode="x unified",
        margin=dict(l=0, r=0, t=8, b=0), height=320,
    )
    fig.update_yaxes(title_text="Spend", secondary_y=False, tickprefix="$",
                     tickformat=",.0s", showgrid=True, gridcolor="rgba(255,255,255,0.05)")
    fig.update_yaxes(title_text="Conversions", secondary_y=True, showgrid=False)
    fig.update_xaxes(showgrid=False)
    st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

# ── Platform comparison table ─────────────────────────────────────────────────
with st.container(border=True):
    st.markdown(
        "<div style='font-size:14px; font-weight:600; margin-bottom:2px;'>Platform Comparison</div>"
        "<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:8px;'>"
        "Sort by any column to find efficiency gaps. The lowest "
        f"<b>{glossary.label('CPA', parenthetical=False)}</b> is the most efficient platform; "
        "use this to inform budget reallocation decisions."
        "</div>",
        unsafe_allow_html=True,
    )

    plat_agg = (
        df_campaigns.groupby("platform")
        .agg(spend=("spend", "sum"),
             conversions=("conversions", "sum"),
             impressions=("impressions", "sum"),
             clicks=("clicks", "sum"))
        .reset_index()
    )
    plat_agg["budget_share"] = (plat_agg["spend"] / plat_agg["spend"].sum() * 100)
    plat_agg["cpa"] = plat_agg["spend"] / plat_agg["conversions"]
    plat_agg["ctr_pct"] = plat_agg["clicks"] / plat_agg["impressions"] * 100
    plat_agg["cpc"] = plat_agg["spend"] / plat_agg["clicks"]
    plat_agg["cpm"] = plat_agg["spend"] / plat_agg["impressions"] * 1000

    plat_agg = plat_agg.rename(columns={
        "platform":     "Platform",
        "spend":        "Spend (USD)",
        "budget_share": "Budget %",
        "conversions":  "Conversions",
        "cpa":          glossary.label("CPA"),
        "ctr_pct":      glossary.label("CTR"),
        "cpc":          glossary.label("CPC"),
        "cpm":          glossary.label("CPM"),
    })

    st.dataframe(
        plat_agg[["Platform", "Spend (USD)", "Budget %", "Conversions",
                  glossary.label("CPA"), glossary.label("CTR"),
                  glossary.label("CPC"), glossary.label("CPM")]],
        width='stretch',
        hide_index=True,
        column_config={
            "Spend (USD)":   st.column_config.NumberColumn(format="$%.2f"),
            "Budget %":      st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
            "Conversions":   st.column_config.NumberColumn(format="%d"),
            glossary.label("CPA"): st.column_config.NumberColumn(format="$%.2f",
                                       help=glossary.definition("CPA")),
            glossary.label("CTR"): st.column_config.NumberColumn(format="%.2f%%",
                                       help=glossary.definition("CTR")),
            glossary.label("CPC"): st.column_config.NumberColumn(format="$%.2f",
                                       help=glossary.definition("CPC")),
            glossary.label("CPM"): st.column_config.NumberColumn(format="$%.2f",
                                       help=glossary.definition("CPM")),
        },
    )

# ── Campaign efficiency scatter ───────────────────────────────────────────────
with st.container(border=True):
    cpa_label = glossary.label("CPA", parenthetical=False)
    st.markdown(
        f"<div style='font-size:14px; font-weight:600; margin-bottom:2px;'>"
        f"Campaign Efficiency — Spend vs {cpa_label}</div>"
        f"<div style='font-size:12px; color:rgba(255,255,255,0.45); margin-bottom:12px;'>"
        f"Each bubble is a campaign. <b>Bubble size</b> represents conversion volume. "
        f"<b>Position</b> shows the relationship between investment (x-axis) and unit cost (y-axis). "
        f"Campaigns in the <span style='color:#FE2C55;'>upper-right are high-spend AND inefficient</span> — "
        f"prime candidates for budget reduction. Campaigns in the "
        f"<span style='color:#34A853;'>lower-left are efficient but under-funded</span> — "
        f"scale these up."
        f"</div>",
        unsafe_allow_html=True,
    )

    df_plot = df_campaigns[df_campaigns["conversions"] > 0].copy()
    df_plot = df_plot.rename(columns={"campaign_name": "Campaign",
                                       "spend": "Spend",
                                       "cpa":   cpa_label,
                                       "conversions": "Conversions"})
    if not df_plot.empty:
        # Median lines for visual quadrants
        median_spend = df_plot["Spend"].median()
        median_cpa   = df_plot[cpa_label].median()

        fig_sc = px.scatter(
            df_plot, x="Spend", y=cpa_label,
            size="Conversions", color="platform",
            hover_name="Campaign",
            color_discrete_map=PLATFORM_COLORS,
            size_max=55, template="plotly_dark",
        )
        # Quadrant lines — solid amber so they stand out against the platform colors
        fig_sc.add_hline(y=median_cpa,  line_dash="dash",
                         line_color="#FBBF24", line_width=1.5,
                         annotation_text=f"<b>Median {cpa_label}</b>",
                         annotation_position="bottom right",
                         annotation_font_size=11,
                         annotation_font_color="#FBBF24",
                         annotation_bgcolor="rgba(0,0,0,0.55)",
                         annotation_borderpad=4)
        fig_sc.add_vline(x=median_spend, line_dash="dash",
                         line_color="#FBBF24", line_width=1.5,
                         annotation_text="<b>Median Spend</b>",
                         annotation_position="top left",
                         annotation_font_size=11,
                         annotation_font_color="#FBBF24",
                         annotation_bgcolor="rgba(0,0,0,0.55)",
                         annotation_borderpad=4)
        fig_sc.update_layout(
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=""),
            margin=dict(l=0, r=0, t=8, b=0), height=420,
            xaxis=dict(title="Spend (USD)", tickprefix="$"),
            yaxis=dict(title=f"{cpa_label} (USD)", tickprefix="$"),
        )
        fig_sc.update_traces(marker=dict(opacity=0.85,
                                          line=dict(width=1, color="rgba(255,255,255,0.25)")))
        st.plotly_chart(fig_sc, width='stretch', config={"displayModeBar": False})

# ── Best / worst callouts (directly under the scatter plot they reference) ────
ranked = df_campaigns[df_campaigns["conversions"] > 0].copy()
if not ranked.empty:
    best  = ranked.nsmallest(1, "cpa").iloc[0]
    worst = ranked.nlargest(1, "cpa").iloc[0]
    bc, wc = st.columns(2)
    with bc:
        st.success(
            f"**Most efficient campaign:** {best['campaign_name']} ({best['platform']}) — "
            f"{fmt_currency(best['cpa'])} {glossary.label('CPA', parenthetical=False)} "
            f"on {fmt_currency(best['spend'])} spend"
        )
    with wc:
        st.error(
            f"**Least efficient campaign:** {worst['campaign_name']} ({worst['platform']}) — "
            f"{fmt_currency(worst['cpa'])} {glossary.label('CPA', parenthetical=False)} "
            f"on {fmt_currency(worst['spend'])} spend"
        )

# ── Platform Strength Overview + Conversion Quality ──────────────────────────
# Two insight charts side by side — each at 50% width.
#
# LEFT:  Platform Strength Radar — normalised 5-dimension spider chart
# RIGHT: Conversion Quality — CTR and Click-to-Conversion rate per platform
#        (the "funnel efficiency" view: where does each platform lose people?)

st.markdown("---")
_radar_col, _conv_col = st.columns(2)

# ── LEFT: Radar ────────────────────────────────────────────────────────────────
with _radar_col:
  with st.container(border=True):
    st.markdown(
        "<div style='font-size:13px; font-weight:700; text-transform:uppercase; "
        "letter-spacing:0.06em; color:rgba(255,255,255,0.8); margin-bottom:3px;'>"
        "Platform Strength Overview</div>"
        "<div style='font-size:11.5px; color:rgba(255,255,255,0.42); margin-bottom:10px;'>"
        "Bigger shape = stronger overall. All axes normalised 0 – 10.</div>",
        unsafe_allow_html=True,
    )
    _pf = plat_agg if not plat_agg.empty else None
    if _pf is not None and len(_pf) >= 2:
        try:
            radar_df = run_query(
                f"""SELECT platform,
                            ROUND(SAFE_DIVIDE(SUM(spend), SUM(conversions)), 4)      AS cpa,
                            ROUND(SAFE_DIVIDE(SUM(clicks), SUM(impressions)), 4)     AS ctr,
                            SUM(conversions)                                         AS conversions,
                            SUM(spend)                                               AS spend,
                            ROUND(SAFE_DIVIDE(SUM(conversions), SUM(impressions)), 6) AS conv_rate
                       FROM `{config.GOLD_REF}`
                      WHERE date >= '{start_date}'
                        AND date <= '{end_date}'
                        AND platform IN ({sql_platforms})
                      GROUP BY platform""",
                max_bytes=config.MAX_BYTES_OVERVIEW,
            )
            if not radar_df.empty and len(radar_df) >= 2:
                dims = ["Cost Efficiency", "Click-Through Rate", "Conversion Rate",
                        "Conversions Volume", "Spend Share"]
                max_cpa   = radar_df["cpa"].max()
                max_ctr   = radar_df["ctr"].max()
                max_cvr   = radar_df["conv_rate"].max()
                max_conv  = radar_df["conversions"].max()
                max_spend = radar_df["spend"].max()
                fig_radar = go.Figure()
                for _, row in radar_df.iterrows():
                    plat = row["platform"]
                    scores = [
                        round((1 - row["cpa"] / max_cpa) * 10, 2) if max_cpa else 0,
                        round((row["ctr"] / max_ctr) * 10, 2) if max_ctr else 0,
                        round((row["conv_rate"] / max_cvr) * 10, 2) if max_cvr else 0,
                        round((row["conversions"] / max_conv) * 10, 2) if max_conv else 0,
                        round((row["spend"] / max_spend) * 10, 2) if max_spend else 0,
                    ]
                    color_hex = config.PLATFORM_COLORS.get(plat, "#888888")
                    try:
                        r_c = int(color_hex[1:3], 16)
                        g_c = int(color_hex[3:5], 16)
                        b_c = int(color_hex[5:7], 16)
                        fill_color = f"rgba({r_c},{g_c},{b_c},0.10)"
                    except Exception:
                        fill_color = "rgba(128,128,128,0.10)"
                    fig_radar.add_trace(go.Scatterpolar(
                        r=scores + [scores[0]], theta=dims + [dims[0]],
                        fill="toself", name=plat,
                        line=dict(color=color_hex, width=2), fillcolor=fill_color,
                        hovertemplate="<b>%{theta}</b><br>Score: %{r:.1f}/10<extra></extra>",
                    ))
                fig_radar.update_layout(
                    template="plotly_dark",
                    polar=dict(
                        bgcolor="rgba(0,0,0,0)",
                        radialaxis=dict(visible=True, range=[0, 10],
                                        gridcolor="rgba(255,255,255,0.1)",
                                        tickfont=dict(size=9, color="rgba(255,255,255,0.4)"),
                                        tickvals=[2, 4, 6, 8, 10]),
                        angularaxis=dict(gridcolor="rgba(255,255,255,0.1)",
                                         tickfont=dict(size=10, color="rgba(255,255,255,0.72)")),
                    ),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    margin=dict(l=55, r=55, t=15, b=40),
                    height=330,
                    legend=dict(orientation="h", yanchor="bottom", y=-0.14,
                               xanchor="center", x=0.5, font=dict(size=11)),
                )
                st.plotly_chart(fig_radar, width='stretch',
                               config={"displayModeBar": False}, key="radar_overview")
                st.caption(
                    "Cost Efficiency inverted — lower Cost per Acquisition = higher score."
                )
        except Exception:
            st.info("Radar chart unavailable — data still loading.")

# ── RIGHT: Conversion Quality Funnel ──────────────────────────────────────────
with _conv_col:
  with st.container(border=True):
    st.markdown(
        "<div style='font-size:13px; font-weight:700; text-transform:uppercase; "
        "letter-spacing:0.06em; color:rgba(255,255,255,0.8); margin-bottom:3px;'>"
        "Conversion Quality</div>"
        "<div style='font-size:11.5px; color:rgba(255,255,255,0.42); margin-bottom:10px;'>"
        "Where does each platform lose people? CTR = impressions → clicks. "
        "CVR = clicks → conversions.</div>",
        unsafe_allow_html=True,
    )
    try:
        funnel_df = run_query(
            f"""SELECT platform,
                        SUM(impressions)                                             AS impressions,
                        SUM(clicks)                                                  AS clicks,
                        SUM(conversions)                                             AS conversions,
                        ROUND(SAFE_DIVIDE(SUM(clicks), SUM(impressions)) * 100, 2)   AS ctr_pct,
                        ROUND(SAFE_DIVIDE(SUM(conversions), SUM(clicks)) * 100, 2)   AS cvr_pct
                   FROM `{config.GOLD_REF}`
                  WHERE date >= '{start_date}'
                    AND date <= '{end_date}'
                    AND platform IN ({sql_platforms})
                  GROUP BY platform
                  ORDER BY platform""",
            max_bytes=config.MAX_BYTES_OVERVIEW,
        )
        if not funnel_df.empty:
            # Grouped bar chart: CTR% and CVR% side by side per platform
            # Two traces — one for each rate — makes the efficiency gap obvious
            fig_funnel = go.Figure()
            fig_funnel.add_trace(go.Bar(
                name="Click-Through Rate (CTR %)",
                x=funnel_df["platform"],
                y=funnel_df["ctr_pct"],
                marker_color=[config.PLATFORM_COLORS.get(p, "#888")
                              for p in funnel_df["platform"]],
                marker_opacity=0.95,
                text=[f"{v:.2f}%" for v in funnel_df["ctr_pct"]],
                textposition="outside",
                textfont=dict(size=11, color="rgba(255,255,255,0.82)"),
                hovertemplate="<b>%{x}</b><br>CTR: %{y:.2f}%<extra></extra>",
                yaxis="y1",
            ))
            fig_funnel.add_trace(go.Bar(
                name="Click-to-Conversion Rate (CVR %)",
                x=funnel_df["platform"],
                y=funnel_df["cvr_pct"],
                marker_color=[config.PLATFORM_COLORS.get(p, "#888")
                              for p in funnel_df["platform"]],
                marker_opacity=0.45,
                marker_pattern_shape="/",
                text=[f"{v:.2f}%" for v in funnel_df["cvr_pct"]],
                textposition="outside",
                textfont=dict(size=11, color="rgba(255,255,255,0.60)"),
                hovertemplate="<b>%{x}</b><br>CVR: %{y:.2f}%<extra></extra>",
                yaxis="y1",
            ))

            # Efficiency insight annotation
            if len(funnel_df) > 0:
                best_ctr = funnel_df.loc[funnel_df["ctr_pct"].idxmax()]
                best_cvr = funnel_df.loc[funnel_df["cvr_pct"].idxmax()]

            fig_funnel.update_layout(
                template="plotly_dark",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                barmode="group",
                bargap=0.20, bargroupgap=0.08,
                yaxis=dict(
                    title="Rate (%)",
                    gridcolor="rgba(255,255,255,0.06)",
                    ticksuffix="%",
                    range=[0, funnel_df[["ctr_pct","cvr_pct"]].max().max() * 1.35],
                ),
                xaxis=dict(gridcolor="rgba(255,255,255,0.0)"),
                legend=dict(orientation="h", yanchor="bottom", y=-0.25,
                           xanchor="center", x=0.5,
                           font=dict(size=10)),
                margin=dict(l=0, r=0, t=15, b=60),
                height=310,
            )
            st.plotly_chart(fig_funnel, width='stretch',
                           config={"displayModeBar": False}, key="funnel_overview")
            # Plain-English insight below the chart
            if len(funnel_df) >= 1:
                best_ctr_p = funnel_df.loc[funnel_df["ctr_pct"].idxmax(), "platform"]
                best_cvr_p = funnel_df.loc[funnel_df["cvr_pct"].idxmax(), "platform"]
                st.caption(
                    f"**{best_ctr_p}** earns the most clicks per impression (highest CTR). "
                    f"**{best_cvr_p}** converts clicks to customers most efficiently (highest CVR). "
                    f"Platforms with high CTR but low CVR have an ad-landing-page mismatch."
                )
    except Exception:
        st.info("Conversion funnel unavailable — data still loading.")

# ── Page-aware chatbot ────────────────────────────────────────────────────────
plat_summary = ", ".join(
    f"{r['platform']} ({fmt_currency(r['spend'])}, "
    f"{fmt_currency(r['spend']/r['conversions'])} CPA)"
    for _, r in plat_agg.assign(
        platform=plat_agg["Platform"], spend=plat_agg["Spend (USD)"],
        conversions=plat_agg["Conversions"]
    ).iterrows()
)

render_page_chatbot(
    page_name="Performance Overview",
    page_summary={
        "Period":            f"{start_date} → {end_date}",
        "Selected platforms": ", ".join(platforms_sel),
        "Total spend":       fmt_currency(total_spend),
        "Total conversions": fmt_number(total_conv),
        "Blended CPA":       fmt_currency(blended_cpa),
        "Blended CTR":       fmt_pct(blended_ctr, decimals=2),
        "Best campaign":     (
            f"{best['campaign_name']} ({best['platform']}, {fmt_currency(best['cpa'])} CPA)"
            if not ranked.empty else "—"
        ),
        "Worst campaign":    (
            f"{worst['campaign_name']} ({worst['platform']}, {fmt_currency(worst['cpa'])} CPA)"
            if not ranked.empty else "—"
        ),
    },
)