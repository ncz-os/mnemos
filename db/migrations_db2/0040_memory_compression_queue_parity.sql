-- migration: 0040_memory_compression_queue_parity
-- target:    IBM Db2 12.1.5 (Oracle Compat) — GA 2026-06-06
-- purpose:   GAP 1 (job 019e7049, CHILD A) — bring the Db2
--            memory_compression_queue to FULL PARITY with the canonical
--            Postgres schema (db/migrations/0040). The original Db2
--            0017_memory_compression_queue.sql shipped a minimal stub
--            (id, memory_id, priority, queued_at, processed_at, status)
--            missing the contest columns owner_id, reason, scoring_profile,
--            attempts, enqueued_at, started_at, finished_at, error.
-- design:    Mirrors db/migrations_oracle/0040 +
--            db/migrations/0040. Architectural law mem_1780005765033 —
--            identical schema + feature set on every hive backend behind
--            the persistence ABC.
--
-- Reconciliation: Db2 has no CREATE TABLE IF NOT EXISTS. On any Db2 that
-- applied the 0017 stub, drop it first (transient work table — pending
-- rows are re-enqueued on demand). The Db2 native CompressionQueueRepository
-- impl + live SKIP LOCKED DATA validation are CHILD D (019e7106-d751);
-- this migration establishes the parity schema it targets.
--
-- Db2 SKIP-LOCKED note (for CHILD D): the contest dequeue uses
--   SELECT ... FOR UPDATE WITH RS USE AND KEEP UPDATE LOCKS / SKIP LOCKED DATA
-- which Db2 12.1.x supports under the Oracle-compat layer.

-- Drop the pre-parity stub if present (ignore error if absent).
DROP TABLE memory_compression_queue;

CREATE TABLE memory_compression_queue (
    id               VARCHAR(36)              NOT NULL,
    memory_id        VARCHAR(36)              NOT NULL,
    owner_id         VARCHAR(255)             NOT NULL DEFAULT 'default',
    reason           VARCHAR(32)              NOT NULL,
    status           VARCHAR(16)              NOT NULL DEFAULT 'pending',
    priority         SMALLINT                 NOT NULL DEFAULT 0,
    scoring_profile  VARCHAR(32)              NOT NULL DEFAULT 'balanced',
    attempts         SMALLINT                 NOT NULL DEFAULT 0,
    enqueued_at      TIMESTAMP(6) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at       TIMESTAMP(6) WITH TIME ZONE,
    finished_at      TIMESTAMP(6) WITH TIME ZONE,
    error            VARCHAR(4000),
    CONSTRAINT mcq_pk PRIMARY KEY (id),
    CONSTRAINT mcq_memory_fk FOREIGN KEY (memory_id)
        REFERENCES memories(id) ON DELETE CASCADE,
    CONSTRAINT mcq_status_valid
        CHECK (status IN ('pending','running','done','failed')),
    CONSTRAINT mcq_reason_valid
        CHECK (reason IN ('on_write','manual','scheduled','reprocess')),
    CONSTRAINT mcq_scoring_profile_valid
        CHECK (scoring_profile IN ('balanced','quality_first','speed_first','custom'))
);

CREATE INDEX idx_mcq_ready
    ON memory_compression_queue (status, priority DESC, enqueued_at);
CREATE INDEX idx_mcq_memory ON memory_compression_queue (memory_id);
CREATE INDEX idx_mcq_owner  ON memory_compression_queue (owner_id);
