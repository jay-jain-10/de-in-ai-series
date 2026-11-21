-- fct_source_quality.sql
-- The article's quality-dimension query: error rate per source system.

SELECT
    f.source_system,
    COUNT(*) AS total_records,
    SUM(CASE WHEN q.flagged_by_layer IS NOT NULL THEN 1 ELSE 0 END) AS flagged_count,
    SUM(CASE WHEN q.flagged_by_layer = 'L1' THEN 1 ELSE 0 END) AS l1_failures,
    SUM(CASE WHEN q.flagged_by_layer = 'L2' THEN 1 ELSE 0 END) AS l2_failures,
    SUM(CASE WHEN q.reviewer_decision = 'confirm_error' THEN 1 ELSE 0 END) AS confirmed_errors,
    ROUND(100.0 * SUM(CASE WHEN q.reviewer_decision = 'confirm_error' THEN 1 ELSE 0 END)
        / NULLIF(SUM(CASE WHEN q.flagged_by_layer IS NOT NULL THEN 1 ELSE 0 END), 0), 2) AS error_rate_pct
FROM {{ source('quality', 'fact_claims') }} f
LEFT JOIN {{ ref('dim_quality') }} q ON f.claim_id = q.record_id
GROUP BY f.source_system
ORDER BY error_rate_pct DESC
