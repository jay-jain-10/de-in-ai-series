-- fct_cost_by_model.sql
-- Share of spend vs share of calls by model; a model with a high cost share but
-- low call share means complexity scoring is sending simple work to expensive tiers.

WITH m AS (
    SELECT model, SUM(cost_usd) AS cost_usd, COUNT(*) AS api_calls
    FROM {{ ref('stg_cost_events') }}
    WHERE NOT cache_hit
    GROUP BY 1
)
SELECT
    model,
    cost_usd,
    api_calls,
    cost_usd / NULLIF(SUM(cost_usd) OVER (), 0)               AS cost_share,
    api_calls::FLOAT / NULLIF(SUM(api_calls) OVER (), 0)      AS call_share
FROM m
ORDER BY cost_usd DESC
