-- Git-Like DAG for Memory Versioning (Phase 3)
-- Extends memory_versions linear log to a proper content-addressed DAG
-- with parent pointers, branches, and merge support.
--
-- All changes additive/non-breaking. Idempotent and safe on live databases.

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- 1. Drop views first so ALTER COLUMN can retype referenced columns
-- ────────────────────────────────────────────────────────────────────────────

DROP VIEW IF EXISTS v_compression_stats CASCADE;
DROP VIEW IF EXISTS v_unreviewed_compressions CASCADE;

-- ────────────────────────────────────────────────────────────────────────────
-- 2. Fix FK type mismatch: compression_quality_log.memory_id (UUID → TEXT)
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE compression_quality_log DROP CONSTRAINT IF EXISTS compression_quality_log_memory_id_fkey;
ALTER TABLE compression_quality_log ALTER COLUMN memory_id TYPE TEXT;
ALTER TABLE compression_quality_log
    ADD CONSTRAINT compression_quality_log_memory_id_fkey
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE;

-- ────────────────────────────────────────────────────────────────────────────
-- 3. DAG columns on memory_versions (additive)
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE memory_versions
    ADD COLUMN IF NOT EXISTS commit_hash       TEXT,
    ADD COLUMN IF NOT EXISTS parent_version_id UUID REFERENCES memory_versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS branch            TEXT NOT NULL DEFAULT 'main',
    ADD COLUMN IF NOT EXISTS merge_parents     UUID[];

-- ────────────────────────────────────────────────────────────────────────────
-- 4. Backfill commit_hash for existing rows (content-addressed, deterministic)
--
-- SHA256 of (memory_id | version_num | content | snapshot_at)
-- Deterministic: same row always produces same hash
-- ────────────────────────────────────────────────────────────────────────────

UPDATE memory_versions
SET commit_hash = encode(
    sha256(
        (memory_id || '|' || version_num::text || '|' || content || '|' || snapshot_at::text)::bytea
    ),
    'hex'
)
WHERE commit_hash IS NULL;

ALTER TABLE memory_versions ALTER COLUMN commit_hash SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_commit_hash ON memory_versions(commit_hash);

-- ────────────────────────────────────────────────────────────────────────────
-- 5. Relax UNIQUE(memory_id, version_num) → branch-scoped partial index
--
-- Allows multiple branches of same memory to have their own version_num
-- Constraint: on 'main' branch, version_num must be strictly increasing per memory
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE memory_versions DROP CONSTRAINT IF EXISTS memory_versions_memory_id_version_num_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_main_linear
    ON memory_versions(memory_id, version_num)
    WHERE branch = 'main';

-- ────────────────────────────────────────────────────────────────────────────
-- 6. memory_branches table: track branch HEAD pointers
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_branches (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id       TEXT        NOT NULL,
    name            TEXT        NOT NULL,
    head_version_id UUID        REFERENCES memory_versions(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      TEXT,
    UNIQUE (memory_id, name)
);

CREATE INDEX IF NOT EXISTS idx_memory_branches_memory ON memory_branches(memory_id);

-- ────────────────────────────────────────────────────────────────────────────
-- 7. Backfill main branch HEAD pointers
--
-- For each memory, find the latest version on 'main' branch and set as HEAD
-- ────────────────────────────────────────────────────────────────────────────

INSERT INTO memory_branches (memory_id, name, head_version_id, created_at)
SELECT DISTINCT ON (mv.memory_id)
    mv.memory_id,
    'main',
    mv.id,
    mv.snapshot_at
FROM memory_versions mv
WHERE mv.branch = 'main'
ORDER BY mv.memory_id, mv.version_num DESC
ON CONFLICT (memory_id, name) DO NOTHING;

-- ────────────────────────────────────────────────────────────────────────────
-- 8. Recreate views with correct types (TEXT for memory_id)
-- ────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_compression_stats AS
SELECT
    COUNT(*) AS total_compressions,
    COUNT(*) FILTER (WHERE reviewed) AS reviewed,
    COUNT(*) FILTER (WHERE NOT reviewed) AS unreviewed,
    AVG(CAST(quality_rating AS FLOAT)) AS avg_quality,
    AVG(compression_ratio) AS avg_ratio
FROM compression_quality_log;

CREATE OR REPLACE VIEW v_unreviewed_compressions AS
SELECT
    cql.id,
    cql.memory_id,
    cql.original_token_count AS original_size,
    cql.compressed_token_count AS compressed_size,
    cql.compression_ratio,
    cql.created AS compressed_at,
    m.category,
    m.content
FROM compression_quality_log cql
LEFT JOIN memories m ON m.id = cql.memory_id
WHERE NOT cql.reviewed
ORDER BY cql.created DESC;

-- ────────────────────────────────────────────────────────────────────────────
-- 9. Session table FK constraints (if sessions migration ran first)
-- ────────────────────────────────────────────────────────────────────────────

-- Safe to run even if sessions table doesn't exist yet
DO $$ BEGIN
    ALTER TABLE session_memory_injections
        ADD CONSTRAINT fk_session_memory_injections_memory
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE SET NULL;
EXCEPTION WHEN undefined_table OR duplicate_object THEN
    NULL;
END $$;

COMMIT;
