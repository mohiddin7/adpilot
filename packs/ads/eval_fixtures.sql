-- Eval-only rows for the tier-2 tables (DuckDB has no pipeline outputs). Loaded by evals/task.py only.
INSERT INTO fct_anomaly_flags VALUES
 ('2024-01-12','Google','G-1','Search_Generic_Terms', 41.20, 22.10, 3.10, 6.20, 1, 'HIGH_CPA', 6.20, 'CRITICAL', 'rolling_14d', 11, 'high', 12),
 ('2024-01-18','TikTok','T-2','Conversion_Focus',     19.80, 10.90, 1.60, 4.70, 1, 'HIGH_CPA', 4.70, 'SEVERE',   'rolling_14d', 14, 'high', 18),
 ('2024-01-21','Facebook','F-3','Traffic_Drive_Jan',   3.10,  7.40, 0.90,-3.60, 1, 'LOW_CPA',  -3.60,'MODERATE', 'day_of_week', 6,  'medium', 21),
 ('2024-01-25','Google','G-4','Display_Remarketing',  15.30,  9.10, 1.10, 3.80, 1, 'HIGH_CPA', 3.80, 'MODERATE', 'rolling_14d', 14, 'high', 25),
 ('2024-01-26','TikTok','T-5','Awareness_GenZ',       10.50, 10.20, 1.50, 0.13, 0, 'NORMAL',   0.13, 'NORMAL',   'rolling_14d', 14, 'high', 26);

INSERT INTO tbl_budget_recommendations VALUES
 ('2024-01-31 06:00:00','2024-01-01','2024-01-30','TikTok',   74266.70, 57.02, 6750, 11.0025, 80000.00, 61.42, 7271.0,  521.0,  'CPA assumed constant at 30-day aggregate average. Constraints: min 10% of total per platform, max 2.0x current spend per platform.'),
 ('2024-01-31 06:00:00','2024-01-01','2024-01-30','Google',   37686.20, 28.94, 4218,  8.9346, 30000.00, 23.03, 3358.0, -860.0,  'CPA assumed constant at 30-day aggregate average. Constraints: min 10% of total per platform, max 2.0x current spend per platform.'),
 ('2024-01-31 06:00:00','2024-01-01','2024-01-30','Facebook', 18292.00, 14.04, 2395,  7.6376, 20244.90, 15.54, 2651.0,  256.0,  'CPA assumed constant at 30-day aggregate average. Constraints: min 10% of total per platform, max 2.0x current spend per platform.');

INSERT INTO tbl_forecast VALUES
 ('2024-01-31','2024-02-01','TikTok','spend',   2480.0, 2100.0, 2860.0, 'holt_winters'),
 ('2024-01-31','2024-02-02','TikTok','spend',   2495.0, 2110.0, 2880.0, 'holt_winters'),
 ('2024-01-31','2024-02-01','Google','spend',   1260.0, 1050.0, 1470.0, 'holt_winters'),
 ('2024-01-31','2024-02-02','Google','spend',   1255.0, 1040.0, 1470.0, 'holt_winters'),
 ('2024-01-31','2024-02-01','Facebook','spend',  610.0,  520.0,  700.0, 'holt_winters'),
 ('2024-01-31','2024-02-02','Facebook','spend',  615.0,  525.0,  705.0, 'holt_winters');
