-- =============================================================================
-- gold_unified_performance_mart.sql
-- Project  : improvado-analytics-lakehouse
-- Status   : DOCUMENTATION — regenerated from SqlBuilder._GOLD_COLUMN_SPEC
--            and _PLATFORM_CONFIG in 02_run_transformations.py.
--
-- Execution flow (run by Script 02, not this file directly):
--   Phase 1: CREATE TABLE IF NOT EXISTS  → DDL schema, NO data, runs once
--   Phase 2: BEGIN TRANSACTION; MERGE×3; COMMIT  → populates / updates data
--            (same MERGE transaction for both first-run and incremental)
--
-- The old CREATE OR REPLACE TABLE ... AS SELECT is GONE.
-- Reason: it destroys NOT NULL constraints and column descriptions every run
-- because BigQuery infers schema from the SELECT and cannot preserve DDL metadata.
-- =============================================================================

-- =============================================================================
-- PHASE 1 — Gold table DDL  (CREATE TABLE IF NOT EXISTS)
-- Idempotent: safe to run on an existing table — it is a no-op.
-- NOT NULL on all 24 base columns (6 dims + 18 metrics).
-- The 6 SAFE_DIVIDE derived metrics are NULLABLE by design:
--   a campaign with zero conversions has cpa = NULL — not zero — which is
--   semantically correct (undefined cost-per-acquisition, not free).
-- =============================================================================
CREATE TABLE IF NOT EXISTS `improvado-analytics-lakehouse.improvado_analytics_production.fct_unified_marketing_performance`
(
  date                       DATE     NOT NULL  OPTIONS(description="Partition date (from bronze ingestion)."),
  platform                   STRING   NOT NULL  OPTIONS(description="Source channel: Facebook, Google, or TikTok."),
  campaign_id                STRING   NOT NULL  OPTIONS(description="Platform campaign identifier."),
  campaign_name              STRING   NOT NULL  OPTIONS(description="Human-readable campaign name."),
  sub_group_id               STRING   NOT NULL  OPTIONS(description="Ad set / ad group / adgroup identifier."),
  sub_group_name             STRING   NOT NULL  OPTIONS(description="Ad set / ad group / adgroup name."),
  impressions                INT64    NOT NULL  OPTIONS(description="Total ad impressions served."),
  clicks                     INT64    NOT NULL  OPTIONS(description="Total measurable ad clicks."),
  spend                      FLOAT64  NOT NULL  OPTIONS(description="Gross ad cost (USD)."),
  conversions                INT64    NOT NULL  OPTIONS(description="Total conversions attributed."),
  conversion_value           FLOAT64  NOT NULL  OPTIONS(description="Revenue from conversions (Google only; 0 elsewhere)."),
  video_views                INT64    NOT NULL  OPTIONS(description="Total video view initiations (TikTok; 0 elsewhere)."),
  video_watch_25             INT64    NOT NULL  OPTIONS(description="Views reaching 25%% completion (TikTok; 0 elsewhere)."),
  video_watch_50             INT64    NOT NULL  OPTIONS(description="Views reaching 50%% completion (TikTok; 0 elsewhere)."),
  video_watch_75             INT64    NOT NULL  OPTIONS(description="Views reaching 75%% completion (TikTok; 0 elsewhere)."),
  video_watch_100            INT64    NOT NULL  OPTIONS(description="Views reaching 100%% completion (TikTok; 0 elsewhere)."),
  likes                      INT64    NOT NULL  OPTIONS(description="Total user likes (TikTok; 0 elsewhere)."),
  shares                     INT64    NOT NULL  OPTIONS(description="Total user shares (TikTok; 0 elsewhere)."),
  comments                   INT64    NOT NULL  OPTIONS(description="Total user comments (TikTok; 0 elsewhere)."),
  reach                      INT64    NOT NULL  OPTIONS(description="Unique audience reached (Facebook; 0 elsewhere)."),
  frequency                  FLOAT64  NOT NULL  OPTIONS(description="Avg impressions per unique user (Facebook; 0 elsewhere)."),
  quality_score              INT64    NOT NULL  OPTIONS(description="Ad relevance score 1-10 (Google; 0 elsewhere)."),
  search_impression_share    FLOAT64  NOT NULL  OPTIONS(description="Share of eligible search impressions (Google; 0 elsewhere)."),
  avg_cpc                    FLOAT64  NOT NULL  OPTIONS(description="Platform-reported avg cost per click (Google; 0 elsewhere)."),
  engagement_rate            FLOAT64           OPTIONS(description="Derived: (likes+shares+comments)/impressions. NULL if impressions=0."),
  cpa                        FLOAT64           OPTIONS(description="Derived: spend/conversions. NULL if conversions=0."),
  ctr                        FLOAT64           OPTIONS(description="Derived: clicks/impressions. NULL if impressions=0."),
  cpc                        FLOAT64           OPTIONS(description="Derived: spend/clicks. NULL if clicks=0."),
  cpm                        FLOAT64           OPTIONS(description="Derived: spend*1000/impressions. NULL if impressions=0."),
  roas                       FLOAT64           OPTIONS(description="Derived: conversion_value/spend. NULL if spend=0 (0 for FB/TT).")
)
PARTITION BY date
CLUSTER BY platform, campaign_id;

-- =============================================================================
-- PHASE 2, STATEMENT 1/4 — Facebook: invalid rows → stg_quarantine_logs
-- =============================================================================
MERGE `improvado-analytics-lakehouse.improvado_analytics_staging.stg_quarantine_logs` AS target
USING (
    SELECT
        TO_HEX(MD5(CONCAT(
            CAST(date AS STRING), '|', 'Facebook', '|',
            campaign_id, '|', sub_group_id, '|', violated_rule_identifier
        )))               AS quarantine_id,
        CURRENT_TIMESTAMP() AS execution_timestamp,
        'facebook_gold_validation'        AS origin_platform,
        TO_JSON_STRING(STRUCT(
            date, campaign_id, sub_group_id,
            spend, impressions, clicks, conversions, conversion_value
        ))                AS raw_record_json,
        violated_rule_identifier,
        'PENDING'         AS remediation_status
    FROM (
        SELECT
            date, campaign_id, sub_group_id,
            spend, impressions, clicks, conversions, conversion_value,
            ARRAY_TO_STRING(
                ARRAY(
                    SELECT rule FROM UNNEST([
                        IF(spend < 0, 'G1_NEGATIVE_SPEND', NULL),
                        IF(impressions < 0, 'G2_NEGATIVE_IMPRESSIONS', NULL),
                        IF(clicks < 0 OR clicks > impressions, 'G3_INVALID_CLICKS', NULL),
                        IF(conversions < 0 OR conversions > clicks, 'G4_INVALID_CONVERSIONS', NULL),
                        IF(conversion_value < 0, 'G5_NEGATIVE_REVENUE', NULL),
                        IF(frequency > 0.0 AND frequency < 1.0, 'G7_INVALID_FREQUENCY', NULL),
                        IF(reach > impressions, 'G10_REACH_EXCEEDS_IMPRESSIONS', NULL)
                    ]) AS rule
                    WHERE rule IS NOT NULL
                ), '|'
            ) AS violated_rule_identifier
        FROM (SELECT
    CAST(date AS DATE)  AS date,
    campaign_id,
    ad_set_id  AS sub_group_id,
    COALESCE(SAFE_CAST(NULLIF(spend, '') AS FLOAT64), 0)  AS spend,
    COALESCE(SAFE_CAST(NULLIF(impressions, '') AS INT64), 0)   AS impressions,
    COALESCE(SAFE_CAST(NULLIF(clicks,      '') AS INT64), 0)   AS clicks,
    COALESCE(SAFE_CAST(NULLIF(conversions, '') AS INT64), 0)   AS conversions,
    CAST(0.0 AS FLOAT64)  AS conversion_value,
    COALESCE(SAFE_CAST(NULLIF(frequency, '') AS FLOAT64), 0)  AS frequency,
    COALESCE(SAFE_CAST(NULLIF(reach, '') AS INT64), 0)  AS reach
FROM `improvado-analytics-lakehouse.improvado_analytics_bronze.facebook_ads_landing`)
    )
    WHERE LENGTH(violated_rule_identifier) > 0
) AS source
ON target.quarantine_id = source.quarantine_id
WHEN NOT MATCHED THEN INSERT ROW;

-- =============================================================================
-- PHASE 2, STATEMENT 2/4 — Google: invalid rows → stg_quarantine_logs
-- =============================================================================
MERGE `improvado-analytics-lakehouse.improvado_analytics_staging.stg_quarantine_logs` AS target
USING (
    SELECT
        TO_HEX(MD5(CONCAT(
            CAST(date AS STRING), '|', 'Google', '|',
            campaign_id, '|', sub_group_id, '|', violated_rule_identifier
        )))               AS quarantine_id,
        CURRENT_TIMESTAMP() AS execution_timestamp,
        'google_gold_validation'        AS origin_platform,
        TO_JSON_STRING(STRUCT(
            date, campaign_id, sub_group_id,
            spend, impressions, clicks, conversions, conversion_value
        ))                AS raw_record_json,
        violated_rule_identifier,
        'PENDING'         AS remediation_status
    FROM (
        SELECT
            date, campaign_id, sub_group_id,
            spend, impressions, clicks, conversions, conversion_value,
            ARRAY_TO_STRING(
                ARRAY(
                    SELECT rule FROM UNNEST([
                        IF(spend < 0, 'G1_NEGATIVE_SPEND', NULL),
                        IF(impressions < 0, 'G2_NEGATIVE_IMPRESSIONS', NULL),
                        IF(clicks < 0 OR clicks > impressions, 'G3_INVALID_CLICKS', NULL),
                        IF(conversions < 0 OR conversions > clicks, 'G4_INVALID_CONVERSIONS', NULL),
                        IF(conversion_value < 0, 'G5_NEGATIVE_REVENUE', NULL),
                        IF(quality_score != 0 AND (quality_score < 1 OR quality_score > 10), 'G6_INVALID_QUALITY_SCORE', NULL),
                        IF(search_impression_share < 0.0 OR search_impression_share > 1.0, 'G9_INVALID_SEARCH_IMPRESSION_SHARE', NULL)
                    ]) AS rule
                    WHERE rule IS NOT NULL
                ), '|'
            ) AS violated_rule_identifier
        FROM (SELECT
    CAST(date AS DATE)  AS date,
    campaign_id,
    ad_group_id  AS sub_group_id,
    COALESCE(SAFE_CAST(NULLIF(cost, '') AS FLOAT64), 0)  AS spend,
    COALESCE(SAFE_CAST(NULLIF(impressions, '') AS INT64), 0)   AS impressions,
    COALESCE(SAFE_CAST(NULLIF(clicks,      '') AS INT64), 0)   AS clicks,
    COALESCE(SAFE_CAST(NULLIF(conversions, '') AS INT64), 0)   AS conversions,
    COALESCE(SAFE_CAST(NULLIF(conversion_value, '') AS FLOAT64), 0)  AS conversion_value,
    COALESCE(SAFE_CAST(NULLIF(quality_score, '') AS INT64), 0)  AS quality_score,
    COALESCE(SAFE_CAST(NULLIF(search_impression_share, '') AS FLOAT64), 0)  AS search_impression_share
FROM `improvado-analytics-lakehouse.improvado_analytics_bronze.google_ads_landing`)
    )
    WHERE LENGTH(violated_rule_identifier) > 0
) AS source
ON target.quarantine_id = source.quarantine_id
WHEN NOT MATCHED THEN INSERT ROW;

-- =============================================================================
-- PHASE 2, STATEMENT 3/4 — TikTok: invalid rows → stg_quarantine_logs
-- =============================================================================
MERGE `improvado-analytics-lakehouse.improvado_analytics_staging.stg_quarantine_logs` AS target
USING (
    SELECT
        TO_HEX(MD5(CONCAT(
            CAST(date AS STRING), '|', 'TikTok', '|',
            campaign_id, '|', sub_group_id, '|', violated_rule_identifier
        )))               AS quarantine_id,
        CURRENT_TIMESTAMP() AS execution_timestamp,
        'tiktok_gold_validation'        AS origin_platform,
        TO_JSON_STRING(STRUCT(
            date, campaign_id, sub_group_id,
            spend, impressions, clicks, conversions, conversion_value
        ))                AS raw_record_json,
        violated_rule_identifier,
        'PENDING'         AS remediation_status
    FROM (
        SELECT
            date, campaign_id, sub_group_id,
            spend, impressions, clicks, conversions, conversion_value,
            ARRAY_TO_STRING(
                ARRAY(
                    SELECT rule FROM UNNEST([
                        IF(spend < 0, 'G1_NEGATIVE_SPEND', NULL),
                        IF(impressions < 0, 'G2_NEGATIVE_IMPRESSIONS', NULL),
                        IF(clicks < 0 OR clicks > impressions, 'G3_INVALID_CLICKS', NULL),
                        IF(conversions < 0 OR conversions > clicks, 'G4_INVALID_CONVERSIONS', NULL),
                        IF(conversion_value < 0, 'G5_NEGATIVE_REVENUE', NULL),
                        IF(NOT(video_watch_25 >= video_watch_50 AND video_watch_50 >= video_watch_75 AND video_watch_75 >= video_watch_100), 'G11_VIDEO_FUNNEL_MONOTONICITY', NULL),
                        IF(video_watch_25 > video_views, 'G12_VIDEO_WATCH_EXCEEDS_VIEWS', NULL)
                    ]) AS rule
                    WHERE rule IS NOT NULL
                ), '|'
            ) AS violated_rule_identifier
        FROM (SELECT
    CAST(date AS DATE)  AS date,
    campaign_id,
    adgroup_id  AS sub_group_id,
    COALESCE(SAFE_CAST(NULLIF(cost, '') AS FLOAT64), 0)  AS spend,
    COALESCE(SAFE_CAST(NULLIF(impressions, '') AS INT64), 0)   AS impressions,
    COALESCE(SAFE_CAST(NULLIF(clicks,      '') AS INT64), 0)   AS clicks,
    COALESCE(SAFE_CAST(NULLIF(conversions, '') AS INT64), 0)   AS conversions,
    CAST(0.0 AS FLOAT64)  AS conversion_value,
    COALESCE(SAFE_CAST(NULLIF(video_views, '') AS INT64), 0)  AS video_views,
    COALESCE(SAFE_CAST(NULLIF(video_watch_25, '') AS INT64), 0)  AS video_watch_25,
    COALESCE(SAFE_CAST(NULLIF(video_watch_50, '') AS INT64), 0)  AS video_watch_50,
    COALESCE(SAFE_CAST(NULLIF(video_watch_75, '') AS INT64), 0)  AS video_watch_75,
    COALESCE(SAFE_CAST(NULLIF(video_watch_100, '') AS INT64), 0)  AS video_watch_100
FROM `improvado-analytics-lakehouse.improvado_analytics_bronze.tiktok_ads_landing`)
    )
    WHERE LENGTH(violated_rule_identifier) > 0
) AS source
ON target.quarantine_id = source.quarantine_id
WHEN NOT MATCHED THEN INSERT ROW;

-- =============================================================================
-- PHASE 2, STATEMENT 4/4 — Populate gold via MERGE transaction
-- Runs on BOTH first-run (empty table) and every incremental update.
-- First run:  all rows are WHEN NOT MATCHED → INSERT.
-- Subsequent: existing keys UPDATE; new date/campaign keys INSERT.
-- Atomicity:  all 3 platform MERGEs are in one BEGIN/COMMIT transaction.
--             If TikTok MERGE fails after Facebook+Google commit, the whole
--             transaction rolls back — no partial-platform state.
-- =============================================================================
BEGIN TRANSACTION;
MERGE `improvado-analytics-lakehouse.improvado_analytics_production.fct_unified_marketing_performance` AS target
USING (
    SELECT
        date, platform, campaign_id, campaign_name, sub_group_id, sub_group_name,
        impressions, clicks, spend, conversions, conversion_value,
        video_views, video_watch_25, video_watch_50, video_watch_75, video_watch_100,
        likes, shares, comments, reach, frequency,
        quality_score, search_impression_share, avg_cpc,
        SAFE_DIVIDE(
            CAST(likes    AS FLOAT64) +
            CAST(shares   AS FLOAT64) +
            CAST(comments AS FLOAT64),
            CAST(impressions AS FLOAT64)
        )                                                     AS engagement_rate,
        SAFE_DIVIDE(spend, CAST(conversions AS FLOAT64))      AS cpa,
        SAFE_DIVIDE(CAST(clicks AS FLOAT64), CAST(impressions AS FLOAT64)) AS ctr,
        SAFE_DIVIDE(spend, CAST(clicks AS FLOAT64))           AS cpc,
        SAFE_DIVIDE(spend * 1000.0, CAST(impressions AS FLOAT64)) AS cpm,
        CAST(0.0 AS FLOAT64)                                                AS roas
    FROM (SELECT
    CAST(date AS DATE)   AS date,
    'Facebook'  AS platform,
    campaign_id,
    campaign_name,
    ad_set_id  AS sub_group_id,
    ad_set_name  AS sub_group_name,
    COALESCE(SAFE_CAST(NULLIF(impressions, '') AS INT64), 0)  AS impressions,
    COALESCE(SAFE_CAST(NULLIF(clicks, '') AS INT64), 0)  AS clicks,
    COALESCE(SAFE_CAST(NULLIF(spend, '') AS FLOAT64), 0)  AS spend,
    COALESCE(SAFE_CAST(NULLIF(conversions, '') AS INT64), 0)  AS conversions,
    CAST(0.0 AS FLOAT64)  AS conversion_value,
    COALESCE(SAFE_CAST(NULLIF(video_views, '') AS INT64), 0)  AS video_views,
    CAST(0 AS INT64)  AS video_watch_25,
    CAST(0 AS INT64)  AS video_watch_50,
    CAST(0 AS INT64)  AS video_watch_75,
    CAST(0 AS INT64)  AS video_watch_100,
    CAST(0 AS INT64)  AS likes,
    CAST(0 AS INT64)  AS shares,
    CAST(0 AS INT64)  AS comments,
    COALESCE(SAFE_CAST(NULLIF(reach, '') AS INT64), 0)  AS reach,
    COALESCE(SAFE_CAST(NULLIF(frequency, '') AS FLOAT64), 0)  AS frequency,
    CAST(0 AS INT64)  AS quality_score,
    CAST(0.0 AS FLOAT64)  AS search_impression_share,
    CAST(0.0 AS FLOAT64)  AS avg_cpc
FROM `improvado-analytics-lakehouse.improvado_analytics_bronze.facebook_ads_landing`)
    WHERE spend >= 0 AND impressions >= 0
      AND clicks >= 0 AND clicks <= impressions
      AND conversions >= 0 AND conversions <= clicks
      AND conversion_value >= 0
      AND (frequency = 0.0 OR frequency >= 1.0) AND reach <= impressions
) AS source
ON  target.date         = source.date
AND target.platform     = source.platform
AND target.campaign_id  = source.campaign_id
AND target.sub_group_id = source.sub_group_id
WHEN MATCHED THEN UPDATE SET
    target.campaign_name = source.campaign_name,
    target.sub_group_name = source.sub_group_name,
    target.impressions = source.impressions,
    target.clicks = source.clicks,
    target.spend = source.spend,
    target.conversions = source.conversions,
    target.conversion_value = source.conversion_value,
    target.video_views = source.video_views,
    target.video_watch_25 = source.video_watch_25,
    target.video_watch_50 = source.video_watch_50,
    target.video_watch_75 = source.video_watch_75,
    target.video_watch_100 = source.video_watch_100,
    target.likes = source.likes,
    target.shares = source.shares,
    target.comments = source.comments,
    target.reach = source.reach,
    target.frequency = source.frequency,
    target.quality_score = source.quality_score,
    target.search_impression_share = source.search_impression_share,
    target.avg_cpc = source.avg_cpc,
    target.engagement_rate = source.engagement_rate,
    target.cpa = source.cpa,
    target.ctr = source.ctr,
    target.cpc = source.cpc,
    target.cpm = source.cpm,
    target.roas = source.roas
WHEN NOT MATCHED THEN INSERT ROW;
MERGE `improvado-analytics-lakehouse.improvado_analytics_production.fct_unified_marketing_performance` AS target
USING (
    SELECT
        date, platform, campaign_id, campaign_name, sub_group_id, sub_group_name,
        impressions, clicks, spend, conversions, conversion_value,
        video_views, video_watch_25, video_watch_50, video_watch_75, video_watch_100,
        likes, shares, comments, reach, frequency,
        quality_score, search_impression_share, avg_cpc,
        SAFE_DIVIDE(
            CAST(likes    AS FLOAT64) +
            CAST(shares   AS FLOAT64) +
            CAST(comments AS FLOAT64),
            CAST(impressions AS FLOAT64)
        )                                                     AS engagement_rate,
        SAFE_DIVIDE(spend, CAST(conversions AS FLOAT64))      AS cpa,
        SAFE_DIVIDE(CAST(clicks AS FLOAT64), CAST(impressions AS FLOAT64)) AS ctr,
        SAFE_DIVIDE(spend, CAST(clicks AS FLOAT64))           AS cpc,
        SAFE_DIVIDE(spend * 1000.0, CAST(impressions AS FLOAT64)) AS cpm,
        SAFE_DIVIDE(conversion_value, spend)                                                AS roas
    FROM (SELECT
    CAST(date AS DATE)   AS date,
    'Google'  AS platform,
    campaign_id,
    campaign_name,
    ad_group_id  AS sub_group_id,
    ad_group_name  AS sub_group_name,
    COALESCE(SAFE_CAST(NULLIF(impressions, '') AS INT64), 0)  AS impressions,
    COALESCE(SAFE_CAST(NULLIF(clicks, '') AS INT64), 0)  AS clicks,
    COALESCE(SAFE_CAST(NULLIF(cost, '') AS FLOAT64), 0)  AS spend,
    COALESCE(SAFE_CAST(NULLIF(conversions, '') AS INT64), 0)  AS conversions,
    COALESCE(SAFE_CAST(NULLIF(conversion_value, '') AS FLOAT64), 0)  AS conversion_value,
    CAST(0 AS INT64)  AS video_views,
    CAST(0 AS INT64)  AS video_watch_25,
    CAST(0 AS INT64)  AS video_watch_50,
    CAST(0 AS INT64)  AS video_watch_75,
    CAST(0 AS INT64)  AS video_watch_100,
    CAST(0 AS INT64)  AS likes,
    CAST(0 AS INT64)  AS shares,
    CAST(0 AS INT64)  AS comments,
    CAST(0 AS INT64)  AS reach,
    CAST(0.0 AS FLOAT64)  AS frequency,
    COALESCE(SAFE_CAST(NULLIF(quality_score, '') AS INT64), 0)  AS quality_score,
    COALESCE(SAFE_CAST(NULLIF(search_impression_share, '') AS FLOAT64), 0)  AS search_impression_share,
    COALESCE(SAFE_CAST(NULLIF(avg_cpc, '') AS FLOAT64), 0)  AS avg_cpc
FROM `improvado-analytics-lakehouse.improvado_analytics_bronze.google_ads_landing`)
    WHERE spend >= 0 AND impressions >= 0
      AND clicks >= 0 AND clicks <= impressions
      AND conversions >= 0 AND conversions <= clicks
      AND conversion_value >= 0
      AND (quality_score = 0 OR (quality_score >= 1 AND quality_score <= 10)) AND search_impression_share >= 0.0 AND search_impression_share <= 1.0
) AS source
ON  target.date         = source.date
AND target.platform     = source.platform
AND target.campaign_id  = source.campaign_id
AND target.sub_group_id = source.sub_group_id
WHEN MATCHED THEN UPDATE SET
    target.campaign_name = source.campaign_name,
    target.sub_group_name = source.sub_group_name,
    target.impressions = source.impressions,
    target.clicks = source.clicks,
    target.spend = source.spend,
    target.conversions = source.conversions,
    target.conversion_value = source.conversion_value,
    target.video_views = source.video_views,
    target.video_watch_25 = source.video_watch_25,
    target.video_watch_50 = source.video_watch_50,
    target.video_watch_75 = source.video_watch_75,
    target.video_watch_100 = source.video_watch_100,
    target.likes = source.likes,
    target.shares = source.shares,
    target.comments = source.comments,
    target.reach = source.reach,
    target.frequency = source.frequency,
    target.quality_score = source.quality_score,
    target.search_impression_share = source.search_impression_share,
    target.avg_cpc = source.avg_cpc,
    target.engagement_rate = source.engagement_rate,
    target.cpa = source.cpa,
    target.ctr = source.ctr,
    target.cpc = source.cpc,
    target.cpm = source.cpm,
    target.roas = source.roas
WHEN NOT MATCHED THEN INSERT ROW;
MERGE `improvado-analytics-lakehouse.improvado_analytics_production.fct_unified_marketing_performance` AS target
USING (
    SELECT
        date, platform, campaign_id, campaign_name, sub_group_id, sub_group_name,
        impressions, clicks, spend, conversions, conversion_value,
        video_views, video_watch_25, video_watch_50, video_watch_75, video_watch_100,
        likes, shares, comments, reach, frequency,
        quality_score, search_impression_share, avg_cpc,
        SAFE_DIVIDE(
            CAST(likes    AS FLOAT64) +
            CAST(shares   AS FLOAT64) +
            CAST(comments AS FLOAT64),
            CAST(impressions AS FLOAT64)
        )                                                     AS engagement_rate,
        SAFE_DIVIDE(spend, CAST(conversions AS FLOAT64))      AS cpa,
        SAFE_DIVIDE(CAST(clicks AS FLOAT64), CAST(impressions AS FLOAT64)) AS ctr,
        SAFE_DIVIDE(spend, CAST(clicks AS FLOAT64))           AS cpc,
        SAFE_DIVIDE(spend * 1000.0, CAST(impressions AS FLOAT64)) AS cpm,
        CAST(0.0 AS FLOAT64)                                                AS roas
    FROM (SELECT
    CAST(date AS DATE)   AS date,
    'TikTok'  AS platform,
    campaign_id,
    campaign_name,
    adgroup_id  AS sub_group_id,
    adgroup_name  AS sub_group_name,
    COALESCE(SAFE_CAST(NULLIF(impressions, '') AS INT64), 0)  AS impressions,
    COALESCE(SAFE_CAST(NULLIF(clicks, '') AS INT64), 0)  AS clicks,
    COALESCE(SAFE_CAST(NULLIF(cost, '') AS FLOAT64), 0)  AS spend,
    COALESCE(SAFE_CAST(NULLIF(conversions, '') AS INT64), 0)  AS conversions,
    CAST(0.0 AS FLOAT64)  AS conversion_value,
    COALESCE(SAFE_CAST(NULLIF(video_views, '') AS INT64), 0)  AS video_views,
    COALESCE(SAFE_CAST(NULLIF(video_watch_25, '') AS INT64), 0)  AS video_watch_25,
    COALESCE(SAFE_CAST(NULLIF(video_watch_50, '') AS INT64), 0)  AS video_watch_50,
    COALESCE(SAFE_CAST(NULLIF(video_watch_75, '') AS INT64), 0)  AS video_watch_75,
    COALESCE(SAFE_CAST(NULLIF(video_watch_100, '') AS INT64), 0)  AS video_watch_100,
    COALESCE(SAFE_CAST(NULLIF(likes, '') AS INT64), 0)  AS likes,
    COALESCE(SAFE_CAST(NULLIF(shares, '') AS INT64), 0)  AS shares,
    COALESCE(SAFE_CAST(NULLIF(comments, '') AS INT64), 0)  AS comments,
    CAST(0 AS INT64)  AS reach,
    CAST(0.0 AS FLOAT64)  AS frequency,
    CAST(0 AS INT64)  AS quality_score,
    CAST(0.0 AS FLOAT64)  AS search_impression_share,
    CAST(0.0 AS FLOAT64)  AS avg_cpc
FROM `improvado-analytics-lakehouse.improvado_analytics_bronze.tiktok_ads_landing`)
    WHERE spend >= 0 AND impressions >= 0
      AND clicks >= 0 AND clicks <= impressions
      AND conversions >= 0 AND conversions <= clicks
      AND conversion_value >= 0
      AND (video_watch_25 >= video_watch_50 AND video_watch_50 >= video_watch_75 AND video_watch_75 >= video_watch_100) AND video_watch_25 <= video_views
) AS source
ON  target.date         = source.date
AND target.platform     = source.platform
AND target.campaign_id  = source.campaign_id
AND target.sub_group_id = source.sub_group_id
WHEN MATCHED THEN UPDATE SET
    target.campaign_name = source.campaign_name,
    target.sub_group_name = source.sub_group_name,
    target.impressions = source.impressions,
    target.clicks = source.clicks,
    target.spend = source.spend,
    target.conversions = source.conversions,
    target.conversion_value = source.conversion_value,
    target.video_views = source.video_views,
    target.video_watch_25 = source.video_watch_25,
    target.video_watch_50 = source.video_watch_50,
    target.video_watch_75 = source.video_watch_75,
    target.video_watch_100 = source.video_watch_100,
    target.likes = source.likes,
    target.shares = source.shares,
    target.comments = source.comments,
    target.reach = source.reach,
    target.frequency = source.frequency,
    target.quality_score = source.quality_score,
    target.search_impression_share = source.search_impression_share,
    target.avg_cpc = source.avg_cpc,
    target.engagement_rate = source.engagement_rate,
    target.cpa = source.cpa,
    target.ctr = source.ctr,
    target.cpc = source.cpc,
    target.cpm = source.cpm,
    target.roas = source.roas
WHEN NOT MATCHED THEN INSERT ROW;
COMMIT TRANSACTION;;