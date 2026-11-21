-- Snowflake: raw landing table for quarantine records + quality dimension.
-- Synced from the quarantine store by src/quality_loader.py (MERGE, idempotent).
CREATE TABLE IF NOT EXISTS QUALITY.quarantine_record (
    quarantine_id       VARCHAR(36) PRIMARY KEY,
    record_id           VARCHAR(100) NOT NULL,
    source_system       VARCHAR(100) NOT NULL,
    flagged_by_layer    VARCHAR(2)   NOT NULL,
    check_id            VARCHAR(200) NOT NULL,
    check_name          VARCHAR(200) NOT NULL,
    anomaly_type        VARCHAR(100),
    confidence          FLOAT,
    severity            VARCHAR(20),
    rules_version       VARCHAR(20),
    run_id              VARCHAR(100),
    flagged_timestamp   TIMESTAMP_NTZ NOT NULL,
    reviewer_decision   VARCHAR(20),
    reviewed_timestamp  TIMESTAMP_NTZ,
    reviewed_by         VARCHAR(100),
    notes               VARCHAR(4000),
    loaded_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    UNIQUE (record_id, check_id)
);

-- Quality dimension: one row per record_id, joinable to any fact table.
CREATE OR REPLACE VIEW QUALITY.dim_quality AS
SELECT
    record_id,
    MAX(source_system)                                          AS source_system,
    CASE WHEN SUM(IFF(flagged_by_layer = 'L1', 1, 0)) > 0 THEN 'L1' ELSE 'L2' END AS flagged_by_layer,
    MIN_BY(check_name, flagged_timestamp)                       AS check_name,
    MAX(anomaly_type)                                           AS anomaly_type,
    MAX(confidence)                                             AS confidence,
    MIN(flagged_timestamp)                                      AS flagged_timestamp,
    CASE
        WHEN SUM(IFF(reviewer_decision = 'confirm_error', 1, 0)) > 0 THEN 'confirm_error'
        WHEN SUM(IFF(reviewer_decision = 'fix_upstream', 1, 0)) > 0  THEN 'fix_upstream'
        WHEN SUM(IFF(reviewer_decision = 'accept_as_is', 1, 0)) > 0  THEN 'accept_as_is'
    END                                                         AS reviewer_decision,
    MAX(reviewed_timestamp)                                     AS reviewed_timestamp,
    MAX(reviewed_by)                                            AS reviewed_by,
    COUNT(*)                                                    AS failed_check_count
FROM QUALITY.quarantine_record
GROUP BY record_id;
