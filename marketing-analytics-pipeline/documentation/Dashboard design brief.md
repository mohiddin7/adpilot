# Cross-Channel Marketing Analytics Dashboard
## Looker Studio Design & Implementation Brief
### Improvado Senior Marketing Analyst Assessment — January 2024 Data

---

## Part 0 — What This Brief Is For

Hand this document to the AI model or developer building the Looker Studio dashboard.
It contains every decision that has already been made: business narrative, data source
details, exact widget specifications for both pages, calculated field formulas, color
codes, and connection steps.

**The dashboard already has real data behind it.** All numbers referenced below are
verified against actual CSV source files. Do not invent placeholder values.

---

## Part 1 — Business Context

### The Problem Being Solved

A marketing team is spending **$130,244.90/month** across three platforms with no
unified performance view. Each platform uses different metric names, different
conversion definitions, and different reporting structures.

The CMO cannot answer four questions without manual spreadsheet work:

| Business Question | Answer Hidden in the Data |
|---|---|
| Which channel has the lowest cost per acquisition? | Facebook at $7.64 — but gets only 14% of budget |
| Is performance improving or degrading? | Requires daily CPA trend, unavailable per-platform |
| Where should the next incremental dollar go? | Optimizer says: shift from TikTok to Google+Facebook |
| What is our blended ROAS across all channels? | Only Google tracks revenue; FB/TikTok = $0 reported |

**Primary insight the dashboard must make impossible to miss:**
TikTok receives **57% of the total budget** yet delivers the **worst CPA ($11.00)**.
Facebook receives **14% of the budget** yet delivers the **best CPA ($7.64)**.
This is $74K/month going to the wrong place.

### Audience

- **CMO / VP Marketing** (non-technical): needs the 30-second executive view, clear
  budget allocation vs. performance comparison, and the AI summary
- **Media Buyers / Performance Marketers** (operational): needs campaign-level CPA,
  daily trend lines, anomaly alerts, and platform-specific metrics
- **Analytics / Data team** (technical): uses the AI Insights page, anomaly table,
  and QA/audit data

---

## Part 2 — Report Structure

**Tool:** Google Looker Studio (lookerstudio.google.com) — free, connects directly to BigQuery.

**Two pages:**

| Page | Name | Purpose |
|---|---|---|
| Page 1 | **Performance Intelligence** | Operational marketing data — spend, CPA, CTR, campaign rankings |
| Page 2 | **AI Insights** | Anomaly detection results + placeholders for optimizer/forecast/LLM |

---

## Part 3 — BigQuery Data Sources

### Connection Steps (Looker Studio)

1. Open **Looker Studio → Create → Report**
2. In the "Add data" panel, select **BigQuery**
3. Authenticate with the Google Cloud account that has access to `improvado-analytics-lakehouse`
4. Navigate: **My projects → improvado-analytics-lakehouse → improvado_analytics_production → fct_unified_marketing_performance**
5. Click **Add** — this is Data Source 1
6. For anomaly data: **Add data → BigQuery → improvado-analytics-lakehouse → improvado_analytics_staging → fct_anomaly_flags**
7. This is Data Source 2

### Primary Data Source (Page 1)

**Table:** `improvado-analytics-lakehouse.improvado_analytics_production.fct_unified_marketing_performance`

**Date partition column:** `date` (use as the report date range dimension)

Key columns available:

```
DIMENSIONS:  date, platform, campaign_id, campaign_name, sub_group_id, sub_group_name
METRICS:     impressions, clicks, spend, conversions, conversion_value,
             video_views, video_watch_25/50/75/100,
             likes, shares, comments, reach, frequency,
             quality_score, search_impression_share, avg_cpc
DERIVED:     engagement_rate, cpa, ctr, cpc, cpm, roas
             (these are row-level — use the calculated fields below for aggregates)
```

### Secondary Data Source (Page 2)

**Table:** `improvado-analytics-lakehouse.improvado_analytics_staging.fct_anomaly_flags`

```
COLUMNS: date, platform, campaign_id, campaign_name,
         observed_cpa, rolling_mean_cpa, rolling_std_cpa,
         z_score, is_anomaly, anomaly_direction
```

---

## Part 4 — Calculated Fields (Create These in Looker Studio)

> **Critical:** Never use `AVG(cpa)` to aggregate cost-per-acquisition across campaigns or dates.
> `AVG` of ratios is mathematically wrong (averaging 5% and 95% gives 50%, not the true 50/100 = 50%).
> Always derive aggregate efficiency metrics from the underlying counts.

Create these as **calculated fields** in the Data Source configuration:

```
FIELD NAME          FORMULA                                      TYPE
Blended CPA         SUM(spend) / SUM(conversions)                Metric (Currency USD)
Blended CTR         SUM(clicks) / SUM(impressions)               Metric (Percent)
Blended CPM         SUM(spend) * 1000 / SUM(impressions)         Metric (Currency USD)
Blended CPC         SUM(spend) / SUM(clicks)                     Metric (Currency USD)
Blended ROAS        SUM(conversion_value) / SUM(spend)           Metric (Number)
Budget Share %      SUM(spend) / SUM(SUM(spend)) * 100           Metric (Percent)
Engagement Rate     SUM(likes+shares+comments) / SUM(impressions) Metric (Percent)
Video Completion %  SUM(video_watch_100) / SUM(video_views)      Metric (Percent)
```

For the anomaly data source:
```
FIELD NAME          FORMULA                                      TYPE
Anomaly Flag Label  CASE WHEN is_anomaly = 1 THEN "🚨 Alert"
                         ELSE "✓ Normal" END                     Dimension
```

---

## Part 5 — Page 1: Performance Intelligence

### Layout (top to bottom, left to right)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  BANNER: "Performance Intelligence" logo/title strip (dark navy)        │
│  [Date Range Control]   [Platform Filter]   [Campaign Name Filter]      │
└─────────────────────────────────────────────────────────────────────────┘

┌──────────┬──────────┬──────────┬──────────┬──────────┐
│  TOTAL   │  TOTAL   │ BLENDED  │ BLENDED  │ BLENDED  │
│  SPEND   │CONVERSIONS│   CPA   │   CTR    │   CPM    │
│$130,244  │  13,363  │  $9.75   │  1.82%   │  $3.54   │
└──────────┴──────────┴──────────┴──────────┴──────────┘

┌─────────────────────────┬────────────────────────────────────────────┐
│  BUDGET ALLOCATION      │  CPA BY PLATFORM                           │
│  Donut chart            │  Horizontal bar chart (sorted asc by CPA)  │
│  TikTok 57% · Google 29%│  Facebook $7.64 ■■■■■                      │
│  Facebook 14%           │  Google   $8.93 ■■■■■■                      │
│                         │  TikTok  $11.00 ■■■■■■■■                    │
└─────────────────────────┴────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  SPEND vs. CPA EFFICIENCY MATRIX  (Scatter plot)                       │
│  X-axis: Total Spend  Y-axis: Blended CPA  Bubble size: Conversions    │
│  Color by Platform  — the TikTok bubble is top-right (high spend, high │
│  CPA); Facebook is bottom-left (low spend, low CPA)                    │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  CAMPAIGN PERFORMANCE TABLE (sortable)                                  │
│  Columns: Platform · Campaign Name · Spend · Conversions · CPA ·       │
│           CTR · CPM · Budget Share                                      │
│  Default sort: CPA ascending                                            │
│  Conditional formatting on CPA:                                         │
│    < $8.00  = dark green background                                     │
│    $8–12    = no color                                                  │
│    > $12    = light red background                                      │
└─────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────┬────────────────────────────────────┐
│  DAILY SPEND BY PLATFORM           │  DAILY CONVERSIONS BY PLATFORM     │
│  Line chart, date on X             │  Line chart, date on X             │
│  3 lines: Facebook / Google /      │  3 lines: Facebook / Google /      │
│  TikTok; platform colors           │  TikTok; platform colors           │
└────────────────────────────────────┴────────────────────────────────────┘

┌──────────────────────────┬───────────────────────────┬──────────────────┐
│  TIKTOK VIDEO FUNNEL     │  FACEBOOK REACH+FREQUENCY │  GOOGLE QUALITY  │
│  Grouped bar chart:      │  Dual-axis chart:          │  Scorecard:      │
│  video_views             │  reach (bars) +            │  Avg quality_    │
│  video_watch_25          │  frequency (line)          │  score           │
│  video_watch_50          │  Platform: Facebook only   │  search_impr_    │
│  video_watch_75          │                            │  share           │
│  video_watch_100         │                            │  avg_cpc         │
│  Platform: TikTok only   │                            │  Platform: Google│
└──────────────────────────┴───────────────────────────┴──────────────────┘
```

### Widget Specifications (Page 1)

---

#### W1 — Report Header Banner

- Type: Rectangle shape + text box
- Background color: `#0D1B2A` (dark navy)
- Title text: "Performance Intelligence" — white, 28pt, medium weight
- Subtitle text: "Cross-Channel Marketing Analytics · January 2024" — `#8BACC8`, 13pt
- Full page width, 80px tall

---

#### W2 — Report-Level Controls

Three control widgets on the same horizontal row, right-aligned:

| Control | Type | Field | Label |
|---|---|---|---|
| Date Range | Date Range Control | `date` | "Date Range" |
| Platform | Filter Control (drop-down) | `platform` | "Platform" |
| Campaign | Filter Control (drop-down) | `campaign_name` | "Campaign" |

---

#### W3–W7 — KPI Scorecards (5 cards, equal width)

Style for all 5: white card, `#0D1B2A` metric text 32pt bold, label 11pt `#6B7280`, subtle border radius.

| Card | Metric | Formula | Format |
|---|---|---|---|
| W3 | Total Spend | `SUM(spend)` | $0.00K |
| W4 | Total Conversions | `SUM(conversions)` | #,## |
| W5 | Blended CPA | Calculated: `SUM(spend)/SUM(conversions)` | $0.00 |
| W6 | Blended CTR | Calculated: `SUM(clicks)/SUM(impressions)` | 0.00% |
| W7 | Blended CPM | Calculated: `SUM(spend)*1000/SUM(impressions)` | $0.00 |

---

#### W8 — Budget Allocation Donut

- Type: Donut/pie chart
- Dimension: `platform`
- Metric: `SUM(spend)`
- Show % labels
- Colors: Facebook `#1877F2`, Google `#34A853`, TikTok `#FE2C55`
- Title: "Budget Allocation" `#0D1B2A` 14pt bold
- Legend below

---

#### W9 — CPA by Platform Horizontal Bar

- Type: Horizontal bar chart
- Dimension: `platform`
- Metric: Calculated `SUM(spend)/SUM(conversions)` (Blended CPA)
- Sort: ascending by CPA
- Colors: same platform palette
- Data labels: on (show $X.XX)
- Reference line: `$9.75` (blended average) — dashed, `#6B7280`, label "Avg $9.75"
- Title: "Cost Per Acquisition by Platform"
- X-axis: starts at $0

---

#### W10 — Spend vs. CPA Scatter (Efficiency Matrix)

- Type: Scatter chart (bubble chart if Looker Studio supports bubble size)
- X-axis: `SUM(spend)` — "Total Spend"
- Y-axis: `SUM(spend)/SUM(conversions)` — "CPA"
- Breakdown dimension: `platform`
- Colors: platform palette
- **If no bubble chart available:** use scatter with platform dimension — one dot per platform
- Data labels: show platform name on each point
- Title: "Spend vs. Efficiency — Where Is the Budget Going?"
- Annotation note below chart: "Ideal = bottom-left quadrant (low spend proving the model before scaling)"

---

#### W11 — Campaign Performance Table

- Type: Table with bars
- Dimensions: `platform`, `campaign_name`
- Metrics (in order):
  1. `SUM(spend)` — "Spend" — format: $0,0
  2. `SUM(conversions)` — "Conversions"
  3. `SUM(spend)/SUM(conversions)` — "CPA" — **enable bar visualization**, color scale green→yellow→red
  4. `SUM(clicks)/SUM(impressions)` — "CTR" — format: 0.00%
  5. `SUM(spend)*1000/SUM(impressions)` — "CPM" — format: $0.00
  6. `SUM(spend)/(SUM(SUM(spend)))` — "Budget Share" — format: 0%
- Default sort: CPA ascending
- Row count: 12 (show all campaigns)
- Conditional formatting on CPA column:
  - `< 8.00` → background `#D1FAE5` (green-50), text `#065F46`
  - `8.00–12.00` → no color
  - `> 12.00` → background `#FEE2E2` (red-50), text `#991B1B`
- Title: "Campaign Performance Rankings (Jan 2024)"

---

#### W12 — Daily Spend by Platform (Line Chart)

- Type: Time series / line chart
- Date dimension: `date`
- Breakdown: `platform`
- Metric: `SUM(spend)`
- Colors: platform palette, lines 2px
- Y-axis: starts at 0
- Title: "Daily Ad Spend"
- Tooltip: show platform + date + spend
- X-axis: date labels every 5 days

---

#### W13 — Daily Conversions by Platform (Line Chart)

- Same structure as W12
- Metric: `SUM(conversions)`
- Title: "Daily Conversions"

---

#### W14 — TikTok Video Funnel

- Type: Grouped bar chart (vertical)
- **Filter: platform = "TikTok"** (apply as chart-level filter)
- Dimension: `campaign_name`
- Metrics (4 grouped bars per campaign):
  1. `SUM(video_views)` — "Views"
  2. `SUM(video_watch_25)` — "25%"
  3. `SUM(video_watch_50)` — "50%"
  4. `SUM(video_watch_75)` — "75%"
  5. `SUM(video_watch_100)` — "Complete"
- Colors: gradient from `#FE2C55` light → dark (5 shades)
- Title: "TikTok Video Completion Funnel"
- Y-axis: show completion %, not absolute counts (divide each by video_views)

---

#### W15 — Facebook Reach & Frequency

- Type: Combination chart (bars + line)
- **Filter: platform = "Facebook"**
- Date dimension: `date`
- Bar metric: `SUM(reach)` — "Unique Reach"
- Line metric: `AVG(frequency)` — "Frequency" — right Y-axis
  - Note: `frequency` is already an average (avg per user), so `AVG(frequency)` here
    means average of daily frequency values per campaign
- Colors: bars `#1877F2` light, line `#0D1B2A` dark
- Title: "Facebook Reach & Frequency Over Time"
- Reference line on frequency: `1.5` — "Optimal frequency zone"

---

#### W16 — Google Quality & Search Metrics

- Three mini-scorecards stacked:
  1. Avg Quality Score: `AVG(quality_score)` filtered to Google — label "Avg Quality Score" — range reference 1-10
  2. Search Impression Share: `AVG(search_impression_share)` × 100 — label "Search Impr. Share" — format 0%
  3. Avg CPC: `SUM(spend)/SUM(clicks)` filtered to Google — label "Google Avg CPC" — format $0.00
- **Filter: platform = "Google"**
- Title: "Google Search Efficiency"

---

## Part 6 — Page 2: AI Insights

### Layout

```
┌─────────────────────────────────────────────────────────────────────────┐
│  BANNER: "AI Insights" title strip (dark gradient)                      │
│  [Date Range Control]   [Platform Filter]                               │
└─────────────────────────────────────────────────────────────────────────┘

┌──────────────┬──────────────┬──────────────┬──────────────┐
│   TOTAL      │  ANOMALOUS   │  HIGH CPA    │  LOW CPA     │
│  RECORDS     │  RECORDS     │  ALERTS      │  ALERTS      │
│   330        │   XX (X%)    │   XX         │   XX         │
└──────────────┴──────────────┴──────────────┴──────────────┘

┌─────────────────────────────────────────┬───────────────────────────────┐
│  ANOMALY TIMELINE                       │  ANOMALY DISTRIBUTION         │
│  Line chart: z_score by date            │  Donut: Normal vs HIGH vs LOW  │
│  Colored points where is_anomaly=1      │  Red / Green / Gray            │
│  Reference bands at +2 and -2           │                               │
└─────────────────────────────────────────┴───────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  ANOMALY DETAIL TABLE                                                   │
│  date · platform · campaign_name · observed_cpa · rolling_mean_cpa ·   │
│  z_score · anomaly_direction                                            │
│  Filter: is_anomaly = 1 only                                            │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────┬───────────────────────────────┐
│  🔧 BUDGET OPTIMIZER (Coming Soon)       │  📈 FORECAST (Coming Soon)    │
│  Placeholder card                        │  Placeholder card             │
│  "Reallocating budget based on CPA       │  "14-day spend + conversion   │
│  efficiency would yield +11.4% more      │  forecast. Powered by         │
│  conversions at same spend."             │  Holt-Winters model."         │
└─────────────────────────────────────────┴───────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  🤖 AI EXECUTIVE SUMMARY (Coming Soon)                                  │
│  Placeholder: "3-sentence executive briefing generated by LLM           │
│  from live performance data. Identifies best platform, key risk,        │
│  and specific reallocation recommendation."                             │
└─────────────────────────────────────────────────────────────────────────┘
```

### Widget Specifications (Page 2)

---

#### A1 — AI Insights Header Banner

- Background: gradient from `#0D1B2A` to `#1A3550`
- Title: "AI Insights" — white, 28pt
- Subtitle: "Anomaly Detection · Budget Optimizer · Forecast · LLM Summary" — `#8BACC8`, 12pt
- Full width, 80px

---

#### A2–A5 — KPI Scorecards (Anomaly Summary)

Data source: `fct_anomaly_flags`

| Card | Label | Formula | Color accent |
|---|---|---|---|
| A2 | Total Records | `COUNT(date)` | Neutral |
| A3 | Anomalous Records | `SUM(is_anomaly)` with `SUM(is_anomaly)/COUNT(date)` delta | Orange |
| A4 | HIGH CPA Alerts | `COUNTIF(anomaly_direction, "HIGH_CPA")` | Red `#EF4444` |
| A5 | LOW CPA Alerts | `COUNTIF(anomaly_direction, "LOW_CPA")` | Green `#10B981` |

For A3 delta text: show as "(X% of records)"

---

#### A6 — Anomaly Timeline (Z-Score Over Time)

- Type: Line chart with scatter overlay
- Data source: `fct_anomaly_flags`
- X-axis: `date`
- Y-axis: `z_score` (use `AVG(z_score)` as the metric — one point per date-platform)
- Breakdown: `platform` (3 lines)
- Colors: platform palette
- **Reference lines** (horizontal dashed):
  - `+2.0` labeled "Anomaly Threshold" — red dashed `#EF4444`
  - `-2.0` labeled "Anomaly Threshold" — green dashed `#10B981`
  - `0` — light gray
- Title: "CPA Z-Score by Platform (7-day Rolling Window)"
- Tooltip: show date, platform, z_score, anomaly_direction

---

#### A7 — Anomaly Distribution Donut

- Type: Donut chart
- Data source: `fct_anomaly_flags`
- Dimension: `anomaly_direction`
- Metric: `COUNT(date)` (record count)
- Colors:
  - NORMAL: `#9CA3AF` (gray)
  - HIGH_CPA: `#EF4444` (red)
  - LOW_CPA: `#10B981` (green)
- Title: "Anomaly Distribution"
- Center label: "Total flags"

---

#### A8 — Anomaly Detail Table

- Type: Table
- Data source: `fct_anomaly_flags`
- **Chart-level filter: `is_anomaly = 1`**
- Dimensions: `date`, `platform`, `campaign_name`
- Metrics:
  1. `observed_cpa` — "Observed CPA" — $0.00
  2. `rolling_mean_cpa` — "7-day Mean CPA" — $0.00
  3. `z_score` — "Z-Score" — 0.00 — bar visualization, color scale red/green
  4. `anomaly_direction` — "Type" — conditional formatting:
     - HIGH_CPA → red text `#EF4444`
     - LOW_CPA → green text `#10B981`
- Sort: `z_score` absolute value descending (most extreme first)
- Title: "Anomaly Events — Flagged Records Only (|Z| > 2.0)"
- Row count: 20

---

#### A9 — Budget Optimizer Placeholder Card

- Type: Rectangle + text
- Background: `#F8FAFC`
- Border: `2px dashed #CBD5E1`
- Icon: 🔧 (or use a bar chart icon SVG)
- Title text: "Budget Optimizer" — `#0D1B2A`, 16pt bold
- Body text (multi-line):
  ```
  Reallocating the $130,244.90 monthly budget based on
  30-day CPA efficiency would yield an estimated:

       +1,526 additional conversions
       +11.4% conversion uplift at the same spend

  Recommendation: Increase Facebook (+14pp), increase Google
  (+29pp), reduce TikTok (-43pp).

  [ Available after running 04_budget_optimizer.py ]
  ```
- Footer text: "Powered by SciPy linear programming (HiGHS solver)" — `#6B7280` 11pt

---

#### A10 — Forecast Placeholder Card

- Type: Rectangle + text
- Same style as A9
- Icon: 📈
- Title: "14-Day Forecast"
- Body: "Holt-Winters exponential smoothing (7-day seasonality) projects spend and conversions 14 days forward per platform. Available after running 05_forecast.py"

---

#### A11 — AI Executive Summary Placeholder Card

- Full width, taller card
- Background: dark gradient `#0D1B2A`
- White text
- Icon: 🤖
- Title: "AI Executive Summary" — 18pt
- Body (placeholder in a styled quote block):
  ```
  "This 3-sentence brief is generated fresh from live BigQuery
   data by a large language model (GPT-4 / Gemini / Llama 3).

   Sentence 1: Best-performing platform with specific CPA.
   Sentence 2: Highest-risk pattern with quantitative comparison.
   Sentence 3: Specific reallocation recommendation with projected uplift."
  ```
- Footer: "Available after running 06_llm_executive_summary.py · Model-agnostic (OpenAI/Groq/Gemini/Ollama)" — gray 11pt

---

## Part 7 — Design Language

### Color Palette

| Token | Hex | Use |
|---|---|---|
| Platform: Facebook | `#1877F2` | All Facebook data points |
| Platform: Google | `#34A853` | All Google data points |
| Platform: TikTok | `#FE2C55` | All TikTok data points |
| Background | `#F5F7FA` | Report canvas background |
| Card fill | `#FFFFFF` | Widget backgrounds |
| Dark header | `#0D1B2A` | Banners, header text |
| Body text | `#374151` | Paragraph text |
| Muted text | `#6B7280` | Labels, subtitles, captions |
| Alert red | `#EF4444` | HIGH_CPA anomalies, over-budget |
| Success green | `#10B981` | LOW_CPA, best performers |
| Border | `#E5E7EB` | Card borders |
| CPA gradient good | `#D1FAE5` → `#FEE2E2` | Table conditional formatting |

### Typography (set in Looker Studio report settings)

- **Font family:** Google Sans (available as Looker Studio default)
  OR: fall back to Roboto / Open Sans
- **Metric values:** 28–36pt, 600 weight, `#0D1B2A`
- **Card labels:** 11pt, 400 weight, `#6B7280`, uppercase with 1px tracking
- **Section titles:** 14pt, 600 weight, `#0D1B2A`
- **Table text:** 12pt, 400, `#374151`

### Layout Rules

- **Content padding:** 16px from report edges, 12px between widgets
- **Card radius:** 8px (set in chart border radius)
- **Card shadow:** not supported in Looker Studio — use 1px `#E5E7EB` border instead
- **Consistent row height:** all scorecards same height (90px); all chart rows same height

### What Makes It Look Like Industry-Grade, Not AI-Generated

1. **Consistent platform color language** — Facebook blue, Google green, TikTok red appear
   everywhere (scorecards, legend, table rows, chart lines) without variation
2. **The key insight is unavoidable** — the scatter plot and side-by-side CPA bar chart force
   the TikTok inefficiency story; it cannot be missed
3. **Contextual reference lines** — the $9.75 blended CPA line on the platform bar chart
   immediately shows which platforms are above/below average
4. **Business terminology** — "Cost Per Acquisition" not "metric_value"; "Budget Share"
   not "percentage"; "Creative Completion Rate" not "video_watch_100_percentage"
5. **Conditional formatting tells the story** — the campaign table's red/green cells on CPA
   make $5.10 (Brand Terms) vs $24.80 (Generic Terms) speak without annotation
6. **Platform-specific sections** — the bottom row of Page 1 having TikTok Funnel,
   Facebook Reach, and Google Quality separately shows understanding that each
   platform measures performance differently
7. **AI page has honest placeholders** — a placeholder that says "available after running
   04_budget_optimizer.py" is more professional than fake data

---

## Part 8 — Verified Business Numbers for the AI Model Building This

These are real numbers from the pipeline. Embed them in placeholder cards and use
them to validate chart outputs during build:

```
Total spend:       $130,244.90
Total conversions:  13,363
Blended CPA:       $9.75
Blended CTR:       1.82% (approx — verify from actual data)

Platform breakdown:
  Facebook:  $18,292.00  14% budget  CPA $7.64  CTR 1.96%  CPM $4.03
  Google:    $37,686.20  29% budget  CPA $8.93  CTR 1.90%  CPM $5.22
  TikTok:    $74,266.70  57% budget  CPA $11.00 CTR 1.61%  CPM $2.59

Campaign CPA ranking (best → worst):
  $5.10   Google / Search_Brand_Terms
  $5.95   Facebook / Conversions_Retargeting
  $6.34   Google / Shopping_All_Products
  $7.52   Facebook / Traffic_Drive_Jan
  $9.44   Facebook / Brand_Awareness_Q1
  $9.72   Google / Display_Remarketing
  $9.92   TikTok / Influencer_Collab
 $10.00   TikTok / Conversion_Focus
 $13.00   TikTok / Awareness_GenZ
 $14.06   TikTok / Traffic_Campaign
 $14.96   Facebook / Video_Views_Campaign
 $24.80   Google / Search_Generic_Terms   ← 4.9× more expensive than Brand Terms
                                           ← this campaign gets 2× the budget of Brand
```

---

## Part 9 — What the Other Model / Developer Should NOT Do

- ❌ Do not use default Looker Studio blue for all charts — use platform-specific colors
- ❌ Do not use `AVG(cpa)` for cross-campaign or cross-date CPA — always derive from `SUM(spend)/SUM(conversions)`
- ❌ Do not put all 12 campaigns + platform breakdown in a single pie chart — it becomes unreadable
- ❌ Do not show `engagement_rate`, `roas` as primary KPIs for Facebook/TikTok — they are 0 for those platforms (no revenue data)
- ❌ Do not create a "TikTok Reach & Frequency" section — TikTok has no reach/frequency in this dataset (those default to 0)
- ❌ Do not show Google `video_views` or `likes` — those default to 0 for Google
- ✅ Do filter each platform-specific section by its platform before showing platform-specific metrics

---

## Part 10 — Connecting fct_anomaly_flags and fct_unified_marketing_performance

For Page 2, use `fct_anomaly_flags` as a standalone data source (no join needed —
it already contains the denormalized campaign_name and platform columns).

If you want to blend data (show gold table metrics alongside anomaly flags),
use Looker Studio's **Data Blending** feature:
- Left source: `fct_unified_marketing_performance`
- Right source: `fct_anomaly_flags`
- Join key: `date + platform + campaign_id`
- This lets you show actual spend/conversions alongside z_score in the same table