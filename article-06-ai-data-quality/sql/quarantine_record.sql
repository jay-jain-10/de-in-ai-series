-- Quarantine / audit log (RDS PostgreSQL in the article's AWS design).
-- Grain: one row per (record_id, check_id). Idempotency key = that pair.
CREATE TABLE IF NOT EXISTS quarantine_record (
    quarantine_id       UUID PRIMARY KEY,
    record_id           VARCHAR(100) NOT NULL,
    source_system       VARCHAR(100) NOT NULL,
    flagged_by_layer    VARCHAR(2)   NOT NULL CHECK (flagged_by_layer IN ('L1', 'L2')),
    check_id            VARCHAR(200) NOT NULL,   -- e.g. L1:claim_amount_range, L2:semantic_consistency
    check_name          VARCHAR(200) NOT NULL,
    anomaly_type        VARCHAR(100),            -- L2 only
    confidence          DOUBLE PRECISION,        -- L2 only
    severity            VARCHAR(20),
    rules_version       VARCHAR(20),
    run_id              VARCHAR(100),
    flagged_timestamp   TIMESTAMP NOT NULL,
    reviewer_decision   VARCHAR(20) CHECK (reviewer_decision IN ('fix_upstream', 'accept_as_is', 'confirm_error')),
    reviewed_timestamp  TIMESTAMP,
    reviewed_by         VARCHAR(100),
    notes               TEXT,
    record_payload      JSONB,
    UNIQUE (record_id, check_id)
);
CREATE INDEX IF NOT EXISTS idx_quarantine_pending
    ON quarantine_record (flagged_timestamp) WHERE reviewer_decision IS NULL;
