-- stg_ticket_classifications.sql
-- Staging model: validates AI output and flags low-confidence classifications
-- for human review.

WITH source AS (
    SELECT * FROM {{ source('raw', 'support_ticket_classifications') }}
),

validated AS (
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
        model_used,
        classified_at,
        loaded_at,

        -- Flag low-confidence for human review queue
        CASE WHEN confidence < 0.7 THEN TRUE ELSE FALSE END AS needs_review,

        -- Flag fallback classifications (AI stage failed entirely)
        CASE WHEN confidence = 0.0 THEN TRUE ELSE FALSE END AS is_fallback,

        -- Days since ticket was created (for SLA tracking)
        DATEDIFF('day', created_at, CURRENT_TIMESTAMP()) AS days_since_created

    FROM source
)

SELECT * FROM validated
