-- fct_cost_anomalies.sql
-- Flags days where pipeline cost exceeds 2x its rolling 7-day average
-- (article: normal $150/day, today $600/day -> investigate).

WITH pipeline_daily AS (
    SELECT created_date, pipeline_name, SUM(cost_usd) AS daily_cost
    FROM {{ ref('stg_cost_events') }}
    GROUP BY 1, 2
),

with_avg AS (
    SELECT
        *,
        AVG(daily_cost) OVER (
            PARTITION BY pipeline_name
            ORDER BY created_date
            ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
        ) AS rolling_7d_avg
    FROM pipeline_daily
)

SELECT
    created_date,
    pipeline_name,
    daily_cost,
    rolling_7d_avg,
    daily_cost / NULLIF(rolling_7d_avg, 0)                    AS variance_ratio,
    daily_cost > {{ var('cost_variance_threshold', 2.0) }} * rolling_7d_avg AS is_anomaly
FROM with_avg
WHERE rolling_7d_avg IS NOT NULL
ORDER BY created_date DESC
