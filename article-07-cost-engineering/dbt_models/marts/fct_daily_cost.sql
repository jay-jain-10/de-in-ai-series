-- fct_daily_cost.sql
-- Daily cost by pipeline / team / model, with cost SLA flags.
-- SLAs: $0.005 per record, $200 per day (article defaults; override via vars).

WITH events AS (
    SELECT * FROM {{ ref('stg_cost_events') }}
),

daily AS (
    SELECT
        created_date,
        pipeline_name,
        team_name,
        model,
        COUNT(*)                                              AS api_calls,
        COUNT(DISTINCT record_id)                             AS records,
        SUM(cost_usd)                                         AS cost_usd,
        SUM(cost_tokens)                                      AS cost_tokens,
        SUM(input_tokens)                                     AS input_tokens,
        SUM(CASE WHEN cache_hit THEN 1 ELSE 0 END)            AS cached_calls,
        SUM(CASE WHEN is_retry THEN 1 ELSE 0 END)             AS retry_calls,
        SUM(CASE WHEN is_retry THEN cost_usd ELSE 0 END)      AS retry_cost_usd,
        SUM(wasted_cost_usd)                                  AS wasted_cost_usd
    FROM events
    GROUP BY 1, 2, 3, 4
)

SELECT
    *,
    cost_usd / NULLIF(records, 0)                             AS cost_per_record_usd,
    cached_calls::FLOAT / NULLIF(api_calls, 0)                AS cache_hit_rate,
    cost_usd / NULLIF(records, 0) > {{ var('max_cost_per_record_usd', 0.005) }} AS breaches_record_sla,
    SUM(cost_usd) OVER (PARTITION BY created_date, pipeline_name)
        > {{ var('max_daily_cost_usd', 200) }}                AS breaches_daily_sla
FROM daily
ORDER BY created_date DESC, cost_usd DESC
