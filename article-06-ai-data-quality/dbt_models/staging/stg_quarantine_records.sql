-- stg_quarantine_records.sql
-- One row per (record_id, check_id) with review status flags.

SELECT
    quarantine_id,
    record_id,
    source_system,
    flagged_by_layer,
    check_id,
    check_name,
    anomaly_type,
    confidence,
    severity,
    rules_version,
    run_id,
    flagged_timestamp,
    reviewer_decision,
    reviewed_timestamp,
    reviewed_by,
    -- Timestamps are stored as UTC TIMESTAMP_NTZ, so compare against SYSDATE()
    -- (UTC, NTZ). CURRENT_TIMESTAMP() is session-timezone LTZ and would skew the
    -- 48h SLA by the session's UTC offset.
    reviewer_decision IS NULL AS is_pending_review,
    DATEDIFF('hour', flagged_timestamp, COALESCE(reviewed_timestamp, SYSDATE())) AS hours_to_review,
    reviewer_decision IS NULL
        AND DATEDIFF('hour', flagged_timestamp, SYSDATE()) > 48 AS breaches_review_sla
FROM {{ source('quality', 'quarantine_record') }}
