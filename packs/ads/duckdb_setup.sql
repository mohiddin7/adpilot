-- Builds the gold mart from the raw CSVs, mirroring pipelines/02_run_transformations.py.
-- {csv_dir} is substituted by the DuckDB connector. Tier-2 tables are created empty so the
-- agent's tools degrade gracefully when the pipeline outputs are not available locally.

CREATE MACRO SAFE_DIVIDE(a, b) AS a / NULLIF(b, 0);

CREATE VIEW fct_unified_marketing_performance AS
WITH base AS (
    SELECT CAST(date AS DATE) AS date, 'Facebook' AS platform, campaign_id, campaign_name,
           ad_set_id AS sub_group_id, ad_set_name AS sub_group_name,
           CAST(impressions AS BIGINT) AS impressions, CAST(clicks AS BIGINT) AS clicks,
           CAST(spend AS DOUBLE) AS spend, CAST(conversions AS BIGINT) AS conversions,
           0.0 AS conversion_value, CAST(video_views AS BIGINT) AS video_views,
           0 AS video_watch_25, 0 AS video_watch_50, 0 AS video_watch_75, 0 AS video_watch_100,
           0 AS likes, 0 AS shares, 0 AS comments,
           CAST(reach AS BIGINT) AS reach, CAST(frequency AS DOUBLE) AS frequency,
           0 AS quality_score, 0.0 AS search_impression_share, 0.0 AS avg_cpc
    FROM read_csv_auto('{csv_dir}/01_facebook_ads.csv')
    UNION ALL
    SELECT CAST(date AS DATE), 'Google', campaign_id, campaign_name, ad_group_id, ad_group_name,
           impressions, clicks, CAST(cost AS DOUBLE), conversions, CAST(conversion_value AS DOUBLE),
           0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0,
           CAST(quality_score AS BIGINT), CAST(search_impression_share AS DOUBLE), CAST(avg_cpc AS DOUBLE)
    FROM read_csv_auto('{csv_dir}/02_google_ads.csv')
    UNION ALL
    SELECT CAST(date AS DATE), 'TikTok', campaign_id, campaign_name, adgroup_id, adgroup_name,
           impressions, clicks, CAST(cost AS DOUBLE), conversions, 0.0,
           video_views, video_watch_25, video_watch_50, video_watch_75, video_watch_100,
           likes, shares, comments, 0, 0.0, 0, 0.0, 0.0
    FROM read_csv_auto('{csv_dir}/03_tiktok_ads.csv')
)
SELECT *,
       SAFE_DIVIDE(likes + shares + comments, impressions)  AS engagement_rate,
       SAFE_DIVIDE(spend, conversions)                      AS cpa,
       SAFE_DIVIDE(clicks, impressions)                     AS ctr,
       SAFE_DIVIDE(spend, clicks)                           AS cpc,
       SAFE_DIVIDE(spend * 1000.0, impressions)             AS cpm,
       SAFE_DIVIDE(conversion_value, spend)                 AS roas
FROM base;

CREATE TABLE fct_anomaly_flags (
    date DATE, platform VARCHAR, campaign_id VARCHAR, campaign_name VARCHAR,
    observed_cpa DOUBLE, rolling_mean_cpa DOUBLE, rolling_std_cpa DOUBLE,
    z_score DOUBLE, is_anomaly INTEGER, anomaly_direction VARCHAR,
    modified_z_score DOUBLE, severity VARCHAR, baseline_method VARCHAR,
    baseline_size INTEGER, confidence VARCHAR, days_of_history INTEGER
);

CREATE TABLE tbl_budget_recommendations (
    generated_at TIMESTAMP, analysis_period_start DATE, analysis_period_end DATE,
    platform VARCHAR, current_spend DOUBLE, current_spend_pct DOUBLE,
    current_conversions DOUBLE, current_cpa DOUBLE,
    recommended_spend DOUBLE, recommended_spend_pct DOUBLE,
    projected_conversions DOUBLE, conversion_delta DOUBLE, assumption_note VARCHAR
);

CREATE TABLE tbl_forecast (
    forecast_execution_date DATE, target_date DATE, platform VARCHAR, metric_name VARCHAR,
    predicted_value DOUBLE, lower_bound DOUBLE, upper_bound DOUBLE, model_used VARCHAR
);
