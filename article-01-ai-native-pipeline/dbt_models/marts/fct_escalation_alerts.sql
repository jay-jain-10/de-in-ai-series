-- fct_escalation_alerts.sql
-- Surfaces tickets needing immediate attention.
-- Only includes high-confidence AI classifications to avoid false alarms.

SELECT
    ticket_id,
    created_at,
    customer_id,
    channel,
    subject,
    sentiment,
    category,
    escalation_risk,
    confidence,
    ai_reasoning,
    days_since_created,
    DATEDIFF('hour', created_at, CURRENT_TIMESTAMP()) AS hours_since_created,

    -- Priority score for alert ordering
    CASE escalation_risk
        WHEN 'critical' THEN 1
        WHEN 'high'     THEN 2
    END AS priority_rank

FROM {{ ref('stg_ticket_classifications') }}

WHERE escalation_risk IN ('high', 'critical')
  AND confidence >= 0.7
  AND NOT is_fallback

ORDER BY priority_rank ASC, created_at ASC
