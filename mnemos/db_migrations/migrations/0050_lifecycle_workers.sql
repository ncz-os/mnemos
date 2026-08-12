-- Parity restatement for PostgreSQL.
--
-- PostgreSQL already has the full lifecycle-worker schema, but it arrived
-- through flat, unnumbered files applied outside the numbered sequence:
--   deletion_requests -> migrations_v4_2_deletion_requests.sql
--   memory_archive    -> migrations_v4_2_persephone.sql
--   deletion_log      -> migrations_v5_1_0_deletion_log.sql
-- Db2, MySQL and Oracle carry the same end state as a numbered
-- 0050_lifecycle_workers.sql, and the parity gate requires all three backend
-- directories to agree on basenames. This file restates that end state
-- idempotently; on any database that ran the flat files it is a no-op.

ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS target_user_id   TEXT;
ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS target_namespace TEXT;
ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS confirmed_at     TIMESTAMPTZ;
ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS soft_deleted_at  TIMESTAMPTZ;
ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS restore_by       TIMESTAMPTZ;
ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS restored_at      TIMESTAMPTZ;
ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS hard_deleted_at  TIMESTAMPTZ;
ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS notes            TEXT;

ALTER TABLE memory_archive ADD COLUMN IF NOT EXISTS archived_by           TEXT    NOT NULL DEFAULT 'system:persephone';
ALTER TABLE memory_archive ADD COLUMN IF NOT EXISTS compression_algo      TEXT    NOT NULL DEFAULT 'zstd';
ALTER TABLE memory_archive ADD COLUMN IF NOT EXISTS original_size_bytes   INTEGER;
ALTER TABLE memory_archive ADD COLUMN IF NOT EXISTS compressed_size_bytes INTEGER;
ALTER TABLE memory_archive ADD COLUMN IF NOT EXISTS schema_version        INTEGER NOT NULL DEFAULT 1;

ALTER TABLE deletion_log ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE deletion_log ADD COLUMN IF NOT EXISTS owner_id     TEXT;
ALTER TABLE deletion_log ADD COLUMN IF NOT EXISTS namespace    TEXT;
ALTER TABLE deletion_log ADD COLUMN IF NOT EXISTS requested_by TEXT;
ALTER TABLE deletion_log ADD COLUMN IF NOT EXISTS request_kind TEXT;
ALTER TABLE deletion_log ADD COLUMN IF NOT EXISTS reason       TEXT;
