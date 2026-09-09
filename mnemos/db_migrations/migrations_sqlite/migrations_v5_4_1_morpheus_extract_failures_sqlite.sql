-- SQLite mirror for migrations_v5_4_1_morpheus_extract_failures.sql.

CREATE TABLE IF NOT EXISTS morpheus_extract_failures (
    memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    status TEXT NOT NULL DEFAULT 'retryable'
        CHECK (status IN ('retryable', 'dead_letter')),
    last_error TEXT NOT NULL,
    last_failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_morpheus_extract_failures_triage
    ON morpheus_extract_failures(status, last_failed_at DESC);
