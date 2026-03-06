-- fct_ticket_sentiment.sql
-- Daily sentiment aggregation for BI dashboards.
-- Excludes fallback classifications to prevent polluting analytics.

WITH tickets AS (
    SELECT * FROM {{ ref('stg_ticket_classifications') }}
    WHERE NOT is_fallback
),

daily_sentiment AS (
    SELECT
        DATE_TRUNC('day', created_at)  AS report_date,
        channel,
        category,
        sentiment,
        escalation_risk,
        COUNT(*)                        AS ticket_count,
        AVG(confidence)                 AS avg_confidence,
        SUM(CASE WHEN needs_review THEN 1 ELSE 0 END) AS needs_review_count
    FROM tickets
    GROUP BY 1, 2, 3, 4, 5
)

SELECT
    report_date,
    channel,
    category,
    sentiment,
    escalation_risk,
    ticket_count,
    ROUND(avg_confidence, 3)           AS avg_confidence,
    needs_review_count,
    -- Percentage of tickets needing human review per segment
    ROUND(
        needs_review_count::FLOAT / NULLIF(ticket_count, 0) * 100, 2
    ) AS review_rate_pct
FROM daily_sentiment
ORDER BY report_date DESC, ticket_count DESC
