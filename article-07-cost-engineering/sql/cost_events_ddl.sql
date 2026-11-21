-- cost_events: one row per Claude API call (incl. retries and cache hits).
-- This is the cost lineage table.
CREATE TABLE IF NOT EXISTS {schema}.cost_events (
    created_at      TIMESTAMP_NTZ,
    created_date    DATE,
    pipeline_name   VARCHAR(100),
    team_name       VARCHAR(100),
    task_type       VARCHAR(50),
    record_id       VARCHAR(100),
    model           VARCHAR(100),
    input_tokens    NUMBER,
    output_tokens   NUMBER,
    cost_tokens     NUMBER,
    cost_usd        FLOAT,
    cache_hit       BOOLEAN,
    batch           BOOLEAN,
    attempt         NUMBER,
    is_retry        BOOLEAN,
    success         BOOLEAN,
    latency_ms      NUMBER,
    loaded_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Article lineage query: which pipeline spent the most in the last 30 days?
-- SELECT pipeline_name, SUM(cost_usd) AS total_cost
-- FROM cost_events
-- WHERE created_date >= DATEADD('day', -30, CURRENT_DATE())
-- GROUP BY pipeline_name ORDER BY total_cost DESC;
