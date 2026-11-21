-- source_risk_score.sql
-- Source risk score (0-100) = error rate per 1000 x business criticality x consequence x 0.8.
-- Mirrors src/risk_sampler.compute_source_risk_score.
-- Tiers: >80 high (100% sampled), 50-80 medium (20%), <50 low (3%).

WITH confirmed AS (
    SELECT
        source_system,
        COUNT_IF(reviewer_decision = 'confirm_error') AS confirmed_errors,
        COUNT(*) AS reviewed
    FROM {{ ref('stg_quarantine_records') }}
    WHERE reviewer_decision IS NOT NULL
    GROUP BY source_system
),
volume AS (
    SELECT source_system, COUNT(*) AS total_records
    FROM {{ source('quality', 'source_volume') }}
    GROUP BY source_system
)

SELECT
    v.source_system,
    1000.0 * COALESCE(c.confirmed_errors, 0) / NULLIF(v.total_records, 0) AS error_rate_per_1000,
    cfg.business_criticality,
    cfg.consequence_severity,
    LEAST(100, 1000.0 * COALESCE(c.confirmed_errors, 0) / NULLIF(v.total_records, 0)
               * cfg.business_criticality * cfg.consequence_severity * 0.8) AS source_risk_score,
    CASE
        WHEN LEAST(100, 1000.0 * COALESCE(c.confirmed_errors, 0) / NULLIF(v.total_records, 0)
                   * cfg.business_criticality * cfg.consequence_severity * 0.8) > 80 THEN 'high'
        WHEN LEAST(100, 1000.0 * COALESCE(c.confirmed_errors, 0) / NULLIF(v.total_records, 0)
                   * cfg.business_criticality * cfg.consequence_severity * 0.8) >= 50 THEN 'medium'
        ELSE 'low'
    END AS risk_tier
FROM volume v
LEFT JOIN confirmed c USING (source_system)
JOIN {{ source('quality', 'source_config') }} cfg USING (source_system)
