-- migrations_v5_4_1_morpheus_extract_failures.sql
--
-- Durable retry/dead-letter state for MORPHEUS EXTRACT. Poison rows keep
-- their success marker unset while no longer monopolizing the candidate cap.

BEGIN;

CREATE TABLE IF NOT EXISTS morpheus_extract_failures (
    memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    status TEXT NOT NULL DEFAULT 'retryable'
        CHECK (status IN ('retryable', 'dead_letter')),
    last_error TEXT NOT NULL,
    last_failed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_morpheus_extract_failures_triage
    ON morpheus_extract_failures(status, last_failed_at DESC);

COMMENT ON TABLE morpheus_extract_failures IS
    'Durable retry and dead-letter state for MORPHEUS EXTRACT manual triage.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mnemos_user') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON morpheus_extract_failures TO mnemos_user';
    END IF;
END $$;

COMMIT;
