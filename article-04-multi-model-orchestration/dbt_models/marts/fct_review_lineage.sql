-- Lineage across models: which reviews triggered aspect extraction, and at what cost.
WITH sentiment_base AS (
    SELECT review_id, model, model_version, cost_usd
    FROM {{ source('reviews', 'review_results') }} WHERE task_type = 'sentiment'
),
aspect_base AS (
    SELECT review_id, model, model_version, cost_usd
    FROM {{ source('reviews', 'review_results') }} WHERE task_type = 'aspect_extraction'
)
SELECT
    s.review_id,
    s.model AS sentiment_model,
    a.model AS aspect_model,
    s.cost_usd + COALESCE(a.cost_usd, 0)      AS total_cost,
    CASE WHEN a.review_id IS NOT NULL THEN 1 ELSE 0 END AS aspect_executed
FROM sentiment_base s
LEFT JOIN aspect_base a ON s.review_id = a.review_id
