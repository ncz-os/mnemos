-- 0061d_morpheus_phase_parity.sql — item 11c Oracle parity.
-- Replay-safe duplicate-column/table/index errors are handled by the Oracle
-- migration applier, matching 0061c_morpheus_runs_parity.sql.

ALTER TABLE memories ADD (consolidated_at TIMESTAMP WITH TIME ZONE);
ALTER TABLE memories ADD (morpheus_run_id VARCHAR2(100));
ALTER TABLE memories ADD (source_memories CLOB CHECK (source_memories IS JSON));
ALTER TABLE memories ADD (provenance VARCHAR2(64));
ALTER TABLE memories ADD (triples_extracted_at TIMESTAMP WITH TIME ZONE);

CREATE TABLE morpheus_extract_failures (
    memory_id VARCHAR2(100) PRIMARY KEY,
    attempts NUMBER(10) DEFAULT 1 NOT NULL CHECK (attempts >= 1),
    status VARCHAR2(16) DEFAULT 'retryable' NOT NULL
        CHECK (status IN ('retryable', 'dead_letter')),
    last_error CLOB NOT NULL,
    last_failed_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_morpheus_extract_failures_triage
    ON morpheus_extract_failures(status, last_failed_at DESC);
