--#SET TERMINATOR @
-- 0061d_morpheus_phase_parity.sql — item 11c Db2 parity.
-- Keep @ as the only terminator; replay-safe duplicate-object SQLSTATEs are
-- handled by the Db2 migration applier, matching 0061c.

ALTER TABLE memories ADD COLUMN consolidated_at TIMESTAMP
@
ALTER TABLE memories ADD COLUMN morpheus_run_id VARCHAR(100)
@
ALTER TABLE memories ADD COLUMN source_memories CLOB(1M)
@
ALTER TABLE memories ADD COLUMN provenance VARCHAR(64)
@
ALTER TABLE memories ADD COLUMN triples_extracted_at TIMESTAMP
@
ALTER TABLE kg_triples ADD COLUMN extracted_by_run_id VARCHAR(100)
@

CREATE TABLE morpheus_extract_failures (
    memory_id VARCHAR(100) NOT NULL PRIMARY KEY,
    attempts BIGINT DEFAULT 1 NOT NULL CHECK (attempts >= 1),
    status VARCHAR(16) DEFAULT 'retryable' NOT NULL
        CHECK (status IN ('retryable', 'dead_letter')),
    last_error CLOB(1M) NOT NULL,
    last_failed_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL
)
@

CREATE INDEX idx_morpheus_extract_failures_triage
    ON morpheus_extract_failures(status, last_failed_at DESC)
@
