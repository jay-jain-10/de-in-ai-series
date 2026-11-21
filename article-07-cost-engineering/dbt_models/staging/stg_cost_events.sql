-- stg_cost_events.sql
-- Cleans the raw cost_events stream and derives retry/waste flags.

SELECT
    created_at,
    created_date,
    pipeline_name,
    COALESCE(team_name, 'unknown')            AS team_name,
    task_type,
    record_id,
    model,
    input_tokens,
    output_tokens,
    cost_tokens,
    cost_usd,
    COALESCE(cache_hit, FALSE)                AS cache_hit,
    COALESCE(batch, FALSE)                    AS batch,
    attempt,
    COALESCE(is_retry, FALSE)                 AS is_retry,
    COALESCE(success, TRUE)                   AS success,
    latency_ms,
    -- spend on calls that produced unusable output (token waste)
    CASE WHEN NOT COALESCE(success, TRUE) THEN cost_usd ELSE 0 END AS wasted_cost_usd
FROM {{ source('raw', 'cost_events') }}
