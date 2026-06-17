"""
Home.py — Executive marketing dashboard (landing page).

Design intent:
  - This is what a senior marketing analyst would build for their CMO.
  - No hardcoded numbers — every metric is queried from BigQuery and refreshed.
  - No "landing page" elements (feature cards, architecture diagrams).
  - Compact number formatting ($130.2K, not $130,244.90 on KPI tiles).
  - Full-form metric labels ("Cost per Acquisition" alongside "CPA").
  - Period-over-period deltas computed dynamically by splitting the
    available date range in half.
"""
import logging

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="AdPilot — Marketing Performance",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

log = logging.getLogger(__name__)

try:
    from lib.bq_client import (
        fetch_top_kpis,
        fetch_platform_summary,
        fetch_period_over_period,
        run_query,
        table_exists,
    )
    from lib.page_style import inject_page_style
    from lib import config, glossary
    from lib.formatters import fmt_currency, fmt_number, fmt_pct, fmt_delta, fmt_currency_full
    from lib.page_chatbot import render_page_chatbot
except ImportError as exc:
    st.error(f"Library import error: {exc}. Run from streamlit_app/ directory.")
    st.stop()

# Apply shared dashboard styling
inject_page_style()

# ── Global CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
html, body, [class*="css"] { font-family: 'Inter', -apple-system, sans-serif; }
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0F1117 0%, #141720 100%);
    border-right: 1px solid rgba(255,255,255,0.07);
}
[data-testid="metric-container"] {
    background: linear-gradient(135deg, #1A1D27 0%, #1E2235 100%);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
    padding: 18px 22px;
    position: relative;
    overflow: hidden;
    min-height: 110px;
}
[data-testid="metric-container"]::before {
    content: '';
    position: absolute; top: 0; left: 0; right: 0;
    height: 3px;
    background: linear-gradient(90deg, #2563EB, #7C3AED);
}
[data-testid="metric-container"] [data-testid="stMetricLabel"] p {
    font-size: 12px !important;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: rgba(255,255,255,0.65) !important;
    font-weight: 500;
}
[data-testid="metric-container"] [data-testid="stMetricValue"] {
    font-size: 32px !important;
    font-weight: 700 !important;
    line-height: 1.2 !important;
}
[data-testid="metric-container"] [data-testid="stMetricDelta"] {
    font-size: 13px !important;
}
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 12px !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
    background: #1A1D27 !important;
}
/* Equal heights for side-by-side cards inside columns */
div[data-testid="column"] > div > [data-testid="stVerticalBlockBorderWrapper"] {
    height: 100%;
}
hr { border-color: rgba(255,255,255,0.07) !important; margin: 1.2rem 0 !important; }
h1, h2, h3 { letter-spacing: -0.02em; }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 3px; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar branding ──────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style='padding: 4px 0 16px 0;'>
        <div style='font-size:10px; color:rgba(255,255,255,0.4); text-transform:uppercase;
                    letter-spacing:0.14em; margin-bottom:4px;'>
            AdPilot
        </div>
        <div style='font-size:18px; font-weight:700; color:#fff; line-height:1.2;'>
            Marketing Performance
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.divider()

# ── Verify gold table exists ──────────────────────────────────────────────────
if not table_exists(config.GOLD_REF):
    st.error(
        "**Gold mart not available.** Run ingestion and transformation first:\n\n"
        "```bash\npython pipelines/01_validate_and_ingest.py --all\n"
        "python pipelines/02_run_transformations.py\n```"
    )
    st.stop()

# ── Fetch live data ────────────────────────────────────────────────────────────
with st.spinner("Loading dashboard…"):
    try:
        kpis     = fetch_top_kpis()
        platforms = fetch_platform_summary()
        deltas   = fetch_period_over_period()
    except Exception as exc:
        st.error(f"Could not load data: {exc}")
        st.stop()

if not kpis:
    st.warning("No data in the gold mart yet.")
    st.stop()

# ── Header strip ──────────────────────────────────────────────────────────────
hdr_l, hdr_r = st.columns([3, 2])
with hdr_l:
    st.markdown("### Marketing Performance")
    period_str = (
        f"{pd.to_datetime(kpis['start_date']).strftime('%b %d, %Y')} – "
        f"{pd.to_datetime(kpis['end_date']).strftime('%b %d, %Y')}"
    )
    st.markdown(
        f"<div style='color:rgba(255,255,255,0.5); font-size:13px; margin-top:-8px;'>"
        f"Period: {period_str} · "
        f"{int(kpis['n_platforms'])} platforms · "
        f"{int(kpis['n_campaigns'])} campaigns"
        f"</div>",
        unsafe_allow_html=True,
    )
with hdr_r:
    st.markdown(
        f"""
        <div style='text-align:right; padding-top:8px;'>
            <div style='font-size:10px; color:rgba(255,255,255,0.4); text-transform:uppercase;
                        letter-spacing:0.12em;'>Data refresh</div>
            <div style='font-size:13px; color:rgba(255,255,255,0.7);'>
                {pd.Timestamp.now(tz='UTC').strftime("%H:%M UTC")} · 5-min cache
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

# ── KPI strip — all from BQ, compact formats, tooltips explain the delta ─────
# Compute the midpoint date so the help text can show exactly what's being compared
_start = pd.to_datetime(kpis["start_date"])
_end   = pd.to_datetime(kpis["end_date"])
_mid   = _start + (_end - _start) / 2
_period_a = f"{_start.strftime('%b %d')} – {_mid.strftime('%b %d')}"
_period_b = f"{(_mid + pd.Timedelta(days=1)).strftime('%b %d')} – {_end.strftime('%b %d')}"

def _delta_help(metric_name: str, definition: str) -> str:
    """Build a help string explaining both the metric AND the delta calculation."""
    return (
        f"{definition}\n\n"
        f"The percentage shows period-over-period change: "
        f"the recent half ({_period_b}) compared to the earlier half ({_period_a}). "
        f"With a single month of data the comparison is intra-month, "
        f"so use it as a directional signal rather than a strict month-over-month metric."
    )

k1, k2, k3, k4 = st.columns(4)

k1.metric(
    label="Total Spend",
    value=fmt_currency(kpis["total_spend"]),
    delta=fmt_delta(deltas.get("spend_delta")) or None,
    help=_delta_help("Total Spend", glossary.definition("Spend")),
)
k2.metric(
    label="Total Conversions",
    value=fmt_number(kpis["total_conversions"]),
    delta=fmt_delta(deltas.get("conversions_delta")) or None,
    help=_delta_help("Total Conversions", glossary.definition("Conversions")),
)
k3.metric(
    label=glossary.label("CPA"),
    value=fmt_currency(kpis["blended_cpa"]),
    # Inverted: lower CPA is better, so flip sign for delta colour
    delta=fmt_delta(-deltas["cpa_delta"]) if deltas.get("cpa_delta") is not None else None,
    delta_color="normal",
    help=_delta_help("Cost per Acquisition", glossary.definition("CPA")) +
         "\n\n(Lower is better — the green arrow means CPA decreased.)",
)
k4.metric(
    label=glossary.label("CTR"),
    value=fmt_pct(kpis["blended_ctr"], decimals=2),
    delta=fmt_delta(deltas.get("ctr_delta")) or None,
    help=_delta_help("Click-Through Rate", glossary.definition("CTR")),
)

st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)

# ── Computed headline strip — data-driven story (no static prose) ─────────────
best_cpa_row  = platforms.loc[platforms["cpa"].idxmin()]
worst_cpa_row = platforms.loc[platforms["cpa"].idxmax()]
cpa_gap_pct   = (worst_cpa_row["cpa"] - best_cpa_row["cpa"]) / best_cpa_row["cpa"] * 100

# Get budget recommendation summary if available
top_rec_text = ""
try:
    if table_exists(config.BUDGET_REF):
        df_rec_summary = run_query(
            f"""SELECT SUM(conversion_delta) AS total_delta,
                       SUM(GREATEST(conversion_delta, 0)) AS gains
               FROM `{config.BUDGET_REF}`""",
            max_bytes=config.MAX_BYTES_OVERVIEW,
        )
        if not df_rec_summary.empty:
            net_delta = df_rec_summary.iloc[0]["total_delta"] or 0
            top_rec_text = f"+{int(net_delta):,} conv. unlocked by reallocation"
except Exception:
    pass

# Get anomaly count
anom_count_text = ""
try:
    if table_exists(config.ANOMALY_REF):
        df_an = run_query(
            f"SELECT COUNT(*) AS n FROM `{config.ANOMALY_REF}` WHERE is_anomaly = 1",
            max_bytes=config.MAX_BYTES_OVERVIEW,
        )
        if not df_an.empty:
            n_anom = int(df_an.iloc[0]["n"])
            anom_count_text = f"{n_anom} anomal{'y' if n_anom == 1 else 'ies'} flagged"
except Exception:
    pass

# Compose headline pieces
headline_pieces = [
    f"<b style='color:{config.PLATFORM_COLORS.get(best_cpa_row['platform'], '#fff')};'>"
    f"{best_cpa_row['platform']}</b> leads efficiency at "
    f"<b>{fmt_currency(best_cpa_row['cpa'])}</b> Cost per Acquisition",
    f"<b style='color:{config.PLATFORM_COLORS.get(worst_cpa_row['platform'], '#fff')};'>"
    f"{worst_cpa_row['platform']}</b> trails at "
    f"<b>{fmt_currency(worst_cpa_row['cpa'])}</b> "
    f"(<span style='color:#FE2C55;'>{cpa_gap_pct:.0f}% higher</span>)",
]
if top_rec_text:
    headline_pieces.append(
        f"<span style='color:#34A853;'>{top_rec_text}</span>"
    )
if anom_count_text:
    headline_pieces.append(
        f"<span style='color:#F59E0B;'>{anom_count_text}</span>"
    )

st.markdown(
    f"""
    <div style='background: linear-gradient(90deg, rgba(37,99,235,0.06), rgba(124,58,237,0.04));
                border-left: 3px solid #2563EB;
                padding: 10px 14px; border-radius: 8px;
                font-size: 13px; line-height: 1.6;
                color: rgba(255,255,255,0.8);
                margin: 8px 0 16px 0;'>
        {' · '.join(headline_pieces)}
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Daily trend (primary chart) ───────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def _daily_trend() -> pd.DataFrame:
    sql = f"""
        SELECT date, platform,
            SUM(spend)       AS spend,
            SUM(conversions) AS conversions
        FROM `{config.GOLD_REF}`
        GROUP BY date, platform
        ORDER BY date
    """
    return run_query(sql, max_bytes=config.MAX_BYTES_OVERVIEW)

df_trend = _daily_trend()

left, right = st.columns([2.2, 1])

with left:
    with st.container(border=True):
        # Computed headline — no static prose
        cheapest_platform = platforms.loc[platforms["cpa"].idxmin(), "platform"]
        spendiest_platform = platforms.iloc[0]["platform"]  # already sorted desc by spend
        st.markdown(
            f"<div style='display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px;'>"
            f"<div style='font-size:11px; color:rgba(255,255,255,0.5); "
            f"text-transform:uppercase; letter-spacing:0.08em;'>Daily Spend by Platform</div>"
            f"<div style='font-size:11px; color:rgba(255,255,255,0.4);'>"
            f"<b style='color:{config.PLATFORM_COLORS.get(spendiest_platform, '#fff')};'>{spendiest_platform}</b> "
            f"leads spend · <b style='color:{config.PLATFORM_COLORS.get(cheapest_platform, '#fff')};'>{cheapest_platform}</b> "
            f"leads efficiency</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Calculate dynamic date boundaries with a +/- 1 day buffer
        min_date = pd.to_datetime(df_trend["date"]).min() - pd.Timedelta(days=1)
        max_date = pd.to_datetime(df_trend["date"]).max() + pd.Timedelta(days=1)

        fig = px.area(
            df_trend, x="date", y="spend", color="platform",
            color_discrete_map=config.PLATFORM_COLORS,
            template="plotly_dark",
        )
        fig.update_layout(
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                        title=""),
            hovermode="x unified",
            margin=dict(l=0, r=0, t=8, b=0),
            height=310,
            yaxis=dict(title="", tickprefix="$"),
            xaxis=dict(title=""),
        )
        fig.update_traces(opacity=0.85)
        st.plotly_chart(fig, width='stretch', config={"displayModeBar": False})

with right:
    with st.container(border=True):
        st.markdown(
            "<div style='font-size:11px; color:rgba(255,255,255,0.5); "
            "text-transform:uppercase; letter-spacing:0.08em; margin-bottom:10px;'>"
            "Spend Allocation"
            "</div>",
            unsafe_allow_html=True,
        )
        fig_donut = go.Figure(go.Pie(
            labels=platforms["platform"],
            values=platforms["spend"],
            hole=0.62,
            marker=dict(
                colors=[config.PLATFORM_COLORS.get(p, "#888") for p in platforms["platform"]],
                line=dict(color="rgba(0,0,0,0)", width=0),
            ),
            textinfo="percent",
            textposition="inside",
            insidetextorientation="horizontal",  # Changed from "radial" to "horizontal" to center text in the arcs
            textfont=dict(size=14, color="white", family="Inter"),
            hovertemplate="<b>%{label}</b><br>%{value:$,.0f}<br>%{percent}<extra></extra>",
            sort=False,
        ))
        fig_donut.update_layout(
            template="plotly_dark",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            showlegend=False,        # we render our own legend below to avoid overlap
            margin=dict(l=0, r=0, t=8, b=8),
            height=280,
            annotations=[dict(
                text=f"<b>{fmt_currency(kpis['total_spend'])}</b><br>"
                     f"<span style='font-size:10px;color:rgba(255,255,255,0.5)'>Total Spend</span>",
                showarrow=False, font=dict(size=18, color="white"),
            )],
        )
        st.plotly_chart(fig_donut, width='stretch', config={"displayModeBar": False})

        # External HTML legend — sits cleanly below the chart, never overlaps
        legend_items = []
        for _, row in platforms.iterrows():
            p = row["platform"]
            c = config.PLATFORM_COLORS.get(p, "#888")
            legend_items.append(
                f"<div style='display:flex; align-items:center; gap:6px; font-size:12px;'>"
                f"<span style='width:10px; height:10px; border-radius:50%; "
                f"background:{c}; display:inline-block;'></span>"
                f"<span style='color:rgba(255,255,255,0.8);'>{p}</span>"
                f"</div>"
            )
        st.markdown(
            f"<div style='display:flex; justify-content:center; gap:18px; "
            f"flex-wrap:wrap; margin-top:4px; margin-bottom:12px; padding:0 8px;'>"  # Added margin-bottom to balance container padding
            f"{''.join(legend_items)}"
            f"</div>",
            unsafe_allow_html=True,
        )

st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

# ── Platform performance grid ─────────────────────────────────────────────────
st.markdown(
    "<div style='font-size:11px; color:rgba(255,255,255,0.5); "
    "text-transform:uppercase; letter-spacing:0.08em; margin-bottom:8px;'>"
    "Platform Performance"
    "</div>",
    unsafe_allow_html=True,
)

# Find best/worst CPA for highlighting
best_cpa  = platforms.loc[platforms["cpa"].idxmin()] if not platforms.empty else None
worst_cpa = platforms.loc[platforms["cpa"].idxmax()] if not platforms.empty else None

plat_cols = st.columns(len(platforms))
for col, (_, row) in zip(plat_cols, platforms.iterrows()):
    platform = row["platform"]
    color = config.PLATFORM_COLORS.get(platform, "#888")
    badge = ""
    if best_cpa is not None and platform == best_cpa["platform"]:
        badge = "<span style='background:rgba(52,168,83,0.15); color:#34A853; font-size:10px; padding:2px 8px; border-radius:10px; font-weight:600;'>BEST CPA</span>"
    elif worst_cpa is not None and platform == worst_cpa["platform"]:
        badge = "<span style='background:rgba(254,44,85,0.15); color:#FE2C55; font-size:10px; padding:2px 8px; border-radius:10px; font-weight:600;'>NEEDS REVIEW</span>"

    spend_share = row["spend"] / kpis["total_spend"] * 100

    with col:
        with st.container(border=True):
            st.markdown(
                f"""
                <div style='padding: 8px 4px 12px 4px;'>
                  <div style='display:flex; align-items:center; justify-content:space-between;
                              min-height: 28px; gap: 8px;'>
                      <div style='font-size:19px; font-weight:700; color:{color};
                                  white-space: nowrap;'>{platform}</div>
                      {badge}
                  </div>
                  <div style='font-size:30px; font-weight:700; margin-top:6px;'>
                      {fmt_currency(row["spend"])}
                  </div>
                  <div style='font-size:12px; color:rgba(255,255,255,0.5); margin-bottom:20px;'>
                      {spend_share:.1f}% of total spend
                  </div>

                  <div style='display:grid;
                              grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
                              gap: 10px 14px;'>
                      <div>
                          <div style='color:rgba(255,255,255,0.5); text-transform:uppercase;
                                      letter-spacing:0.05em; font-size:10px; line-height:1.3;
                                      margin-bottom: 4px;'>
                              Conversions
                          </div>
                          <div style='font-size:17px; font-weight:600;'>
                              {fmt_number(row["conversions"])}
                          </div>
                      </div>
                      <div>
                          <div style='color:rgba(255,255,255,0.5); text-transform:uppercase;
                                      letter-spacing:0.05em; font-size:10px; line-height:1.3;
                                      margin-bottom: 4px;'>
                              Cost per Acquisition
                          </div>
                          <div style='font-size:17px; font-weight:600;'>
                              {fmt_currency(row["cpa"])}
                          </div>
                      </div>
                      <div>
                          <div style='color:rgba(255,255,255,0.5); text-transform:uppercase;
                                      letter-spacing:0.05em; font-size:10px; line-height:1.3;
                                      margin-bottom: 4px;'>
                              Click-Through Rate
                          </div>
                          <div style='font-size:17px; font-weight:600;'>
                              {fmt_pct(row["ctr"], decimals=2)}
                          </div>
                      </div>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)

# ── Things needing attention ──────────────────────────────────────────────────
attn_l, attn_r = st.columns(2)

# Top opportunity from budget optimizer
with attn_l:
    with st.container(border=True):
        st.markdown(
            "<div style='font-size:11px; color:rgba(255,255,255,0.5); "
            "text-transform:uppercase; letter-spacing:0.08em;'>"
            "🎯 Top Opportunity"
            "</div>",
            unsafe_allow_html=True,
        )

        if table_exists(config.BUDGET_REF):
            try:
                df_bud = run_query(
                    f"""SELECT platform, current_spend_pct, recommended_spend_pct,
                              conversion_delta
                       FROM `{config.BUDGET_REF}`
                       ORDER BY ABS(conversion_delta) DESC LIMIT 1""",
                    max_bytes=config.MAX_BYTES_OVERVIEW,
                )
                if not df_bud.empty:
                    r = df_bud.iloc[0]
                    direction = "increase" if r["recommended_spend_pct"] > r["current_spend_pct"] else "reduce"
                    color = "#34A853" if direction == "increase" else "#FE2C55"
                    st.markdown(
                        f"""
                        <div style='font-size:14px; line-height:1.6; padding:4px 8px 12px 8px;'>
                            <b>{direction.title()} {r['platform']}</b> from
                            <span style='color:rgba(255,255,255,0.5);'>{r['current_spend_pct']:.0f}%</span>
                            <span style='color:rgba(255,255,255,0.4);'>→</span>
                            <span style='color:{color}; font-weight:700;'>{r['recommended_spend_pct']:.0f}%</span>
                            <br/>
                            <span style='font-size:13px; color:rgba(255,255,255,0.55);'>
                                Projected conversion impact:
                                <b style='color:{color};'>{fmt_delta(r['conversion_delta'], as_pct=False)}</b>
                            </span>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                else:
                    st.info("Budget optimizer hasn't run yet.")
            except Exception:
                st.info("Budget recommendations not available.")
        else:
            st.info("Run `pipelines/04_budget_optimizer.py` to populate this card.")

# Top anomaly
with attn_r:
    with st.container(border=True):
        st.markdown(
            "<div style='font-size:11px; color:rgba(255,255,255,0.5); "
            "text-transform:uppercase; letter-spacing:0.08em;'>"
            "⚠️ Most Severe Anomaly"
            "</div>",
            unsafe_allow_html=True,
        )

        if table_exists(config.ANOMALY_REF):
            try:
                df_a = run_query(
                    f"""SELECT date, platform, campaign_name, observed_cpa,
                              rolling_mean_cpa, z_score, anomaly_direction
                       FROM `{config.ANOMALY_REF}`
                       WHERE is_anomaly = 1
                       ORDER BY ABS(z_score) DESC LIMIT 1""",
                    max_bytes=config.MAX_BYTES_OVERVIEW,
                )
                if not df_a.empty:
                    r = df_a.iloc[0]
                    direction = (r['anomaly_direction'] or '').replace('_', ' ').title()
                    color = "#FE2C55" if "HIGH" in (r['anomaly_direction'] or "") else "#F59E0B"
                    st.markdown(
                        f"""
                        <div style='font-size:14px; line-height:1.6; padding:4px 8px 12px 8px;'>
                            <b>{r['campaign_name']}</b>
                            <span style='font-size:11px; color:rgba(255,255,255,0.4);
                                         border:1px solid rgba(255,255,255,0.15);
                                         padding:1px 6px; border-radius:8px; margin-left:6px;'>
                                {r['platform']}
                            </span>
                            <br/>
                            <span style='font-size:13px; color:rgba(255,255,255,0.55);'>
                                {direction} — observed
                                <b style='color:{color};'>{fmt_currency_full(r['observed_cpa'])}</b>
                                vs rolling mean
                                <b>{fmt_currency_full(r['rolling_mean_cpa'])}</b>
                                (z = {r['z_score']:.2f})
                            </span>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                else:
                    st.success("No anomalies detected.")
            except Exception:
                st.info("Anomaly data not available.")
        else:
            st.info("Run `pipelines/03_anomaly_detection.py` to populate this card.")

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(
    "<div style='font-size:11px; color:rgba(255,255,255,0.25); text-align:center; "
    "margin-top:24px;'>"
    "All metrics are computed live from BigQuery · 5-minute cache · "
    "Open a deeper view via the sidebar"
    "</div>",
    unsafe_allow_html=True,
)

# ── Render the floating page-context chatbot ──────────────────────────────────
render_page_chatbot(
    page_name="Home",
    page_summary={
        "Period":               period_str,
        "Total spend":          fmt_currency(kpis["total_spend"]),
        "Total conversions":    fmt_number(kpis["total_conversions"]),
        "Blended Cost/Acq":     fmt_currency(kpis["blended_cpa"]),
        "Blended CTR":          fmt_pct(kpis["blended_ctr"], decimals=2),
        "Best CPA platform":    (
            f"{best_cpa['platform']} ({fmt_currency(best_cpa['cpa'])})"
            if best_cpa is not None else "—"
        ),
        "Worst CPA platform":   (
            f"{worst_cpa['platform']} ({fmt_currency(worst_cpa['cpa'])})"
            if worst_cpa is not None else "—"
        ),
        "Platforms":            int(kpis["n_platforms"]),
        "Campaigns":            int(kpis["n_campaigns"]),
    },
)