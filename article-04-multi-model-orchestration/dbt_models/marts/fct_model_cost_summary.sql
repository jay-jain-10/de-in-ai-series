-- Cost as metadata: per task/model cost and latency from the model_calls log table.
-- Alert when a model's share of calls jumps (e.g. Opus 5% -> 60%).
SELECT
    task,
    model,
    COUNT(*)                                  AS call_count,
    SUM(cost_usd)                             AS total_cost,
    AVG(cost_usd)                             AS avg_cost_per_call,
    -- ordered-set aggregate (Snowflake / Redshift / Postgres); the two-arg form is
    -- analytic-only in BigQuery and is invalid alongside GROUP BY
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency,
    AVG(CASE WHEN cache_hit THEN 1.0 ELSE 0 END) AS cache_hit_rate
FROM {{ source('reviews', 'model_calls') }}
GROUP BY task, model
