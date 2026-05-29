-- migration: 0040_memory_compression_queue_parity
-- target:    PostgreSQL (canonical backend)
-- purpose:   GAP 1 (job 019e7049, CHILD A) — parity anchor for the
--            backend-agnostic compression queue. Postgres already ships
--            the full schema in db/migrations_v3_1_compression.sql; this
--            migration is the NNNN-numbered, idempotent equivalent so the
--            three backend migration trees (db/migrations,
--            db/migrations_oracle, db/migrations_db2) carry the same
--            0040 basename (migration-parity-check) and a fresh bootstrap
--            lands an identical memory_compression_queue everywhere.
-- design:    architectural law mem_1780005765033 — identical schema +
--            feature set on every hive backend behind the persistence ABC.
--
-- Idempotent: CREATE ... IF NOT EXISTS — a no-op on any Postgres that
-- already ran the v3.1 compression migration.

CREATE TABLE IF NOT EXISTS memory_compression_queue (
    id                UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id         TEXT          NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    owner_id          TEXT          NOT NULL DEFAULT 'default',
    reason            VARCHAR(32)   NOT NULL,
    status            VARCHAR(16)   NOT NULL DEFAULT 'pending',
    priority          SMALLINT      NOT NULL DEFAULT 0,
    scoring_profile   VARCHAR(32)   NOT NULL DEFAULT 'balanced',
    attempts          SMALLINT      NOT NULL DEFAULT 0,
    enqueued_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    error             TEXT,

    CONSTRAINT mcq_status_valid CHECK (status IN ('pending','running','done','failed')),
    CONSTRAINT mcq_reason_valid CHECK (reason IN ('on_write','manual','scheduled','reprocess')),
    CONSTRAINT mcq_scoring_profile_valid
        CHECK (scoring_profile IN ('balanced','quality_first','speed_first','custom'))
);

CREATE INDEX IF NOT EXISTS idx_mcq_ready
    ON memory_compression_queue(status, priority DESC, enqueued_at)
    WHERE status IN ('pending','running');
CREATE INDEX IF NOT EXISTS idx_mcq_memory ON memory_compression_queue(memory_id);
CREATE INDEX IF NOT EXISTS idx_mcq_owner  ON memory_compression_queue(owner_id);
