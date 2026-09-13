-- Item 11c: columns and retry state used by CONSOLIDATE/SYNTHESISE/EXTRACT.
-- MySQL has no ADD COLUMN IF NOT EXISTS. The migration ledger applies this
-- file once; MysqlBackend.open() independently uses information_schema guards
-- to repair legacy or partially provisioned installations.
ALTER TABLE memories ADD COLUMN morpheus_run_id CHAR(36);
ALTER TABLE memories ADD COLUMN source_memories JSON;
ALTER TABLE memories ADD COLUMN provenance VARCHAR(64);
ALTER TABLE memories ADD COLUMN triples_extracted_at DATETIME(6);
ALTER TABLE kg_triples ADD COLUMN extracted_by_run_id CHAR(36);

CREATE TABLE IF NOT EXISTS morpheus_extract_failures (
    memory_id VARCHAR(64) NOT NULL PRIMARY KEY,
    attempts INT NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'retryable',
    last_error TEXT NOT NULL,
    last_failed_at DATETIME(6) NOT NULL DEFAULT NOW(6),
    KEY idx_morpheus_extract_failures_triage (status, last_failed_at DESC),
    CONSTRAINT morpheus_extract_failures_attempts CHECK (attempts >= 1),
    CONSTRAINT morpheus_extract_failures_status
        CHECK (status IN ('retryable', 'dead_letter'))
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
