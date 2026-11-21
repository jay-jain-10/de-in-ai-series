-- dim_quality.sql
-- Quality dimension, grain = one row per record_id. Join to any fact table on record_id.
-- A record may fail several checks; they are collapsed here (per-check detail stays
-- in stg_quarantine_records) so joins do not fan out fact rows.

SELECT
    record_id,
    MAX(source_system) AS source_system,
    CASE WHEN COUNT_IF(flagged_by_layer = 'L1') > 0 THEN 'L1' ELSE 'L2' END AS flagged_by_layer,
    MIN_BY(check_name, flagged_timestamp) AS check_name,
    MAX(anomaly_type) AS anomaly_type,
    MAX(confidence) AS confidence,
    MIN(flagged_timestamp) AS flagged_timestamp,
    CASE
        WHEN COUNT_IF(reviewer_decision = 'confirm_error') > 0 THEN 'confirm_error'
        WHEN COUNT_IF(reviewer_decision = 'fix_upstream') > 0 THEN 'fix_upstream'
        WHEN COUNT_IF(reviewer_decision = 'accept_as_is') > 0 THEN 'accept_as_is'
    END AS reviewer_decision,
    MAX(reviewed_timestamp) AS reviewed_timestamp,
    MAX(reviewed_by) AS reviewed_by,
    COUNT(*) AS failed_check_count
FROM {{ ref('stg_quarantine_records') }}
GROUP BY record_id
