-- migrations_v6_3_morpheus_runs_parity_sqlite.sql
--
-- Item 11/11a: bring SQLite's morpheus_runs table onto the canonical
-- Postgres shape so the new MorpheusRepository ABC can run identically
-- on every backend.
--
-- Canonical columns (see mnemos/db_migrations/migrations_v3_3_morpheus.sql
-- + migrations_v3_3_morpheus_namespace.sql + migrations_v4_2_morpheus_consolidate.sql
-- + migrations_v4_2_morpheus_extract.sql):
--   id, started_at, finished_at, status, phase, triggered_by,
--   window_started_at, window_ended_at, window_hours, cluster_min_size,
--   memories_scanned, clusters_found, summaries_created,
--   memories_consolidated, clusters_consolidated,
--   triples_extracted, memories_processed_for_extraction,
--   error, config, namespace
--
-- SQLite's pre-11a shape was a STUB from an early design:
--   id, owner_id, namespace (NOT NULL DEFAULT 'default'),
--   status (DEFAULT 'pending'), config, started_at, finished_at, error
-- missing 11 columns Postgres carries and using `pending' as the default
-- status (not a valid value in Postgres's CHECK constraint).
--
-- The legacy `owner_id` column is dead — no SQL outside this table
-- references it — so we drop it in the same migration. Postgres never
-- had it; aligning here keeps the ABC surface uniform across backends.
--
-- The legacy `namespace` column is NOT NULL DEFAULT 'default'; on
-- Postgres it's nullable with NULL = "all namespaces" (load-bearing for
-- phase_replay's "OR r.namespace IS NULL" check). The NOT NULL
-- constraint blocks the ABC's begin_run from inserting NULL, so we
-- rebuild the column nullable.
--
-- Idempotency: this migration marks itself applied via a `schema_marker`
-- row. The Python migration runner in `mnemos/persistence/sqlite.py`
-- (see `_apply_migrations`) consults `schema_marker` and skips the
-- migration file when the marker is set, so the destructive body
-- (RENAME COLUMN, DROP COLUMN, etc.) never runs twice on the same DB.
-- The first apply leaves the marker set; subsequent open() calls skip
-- the whole file.

CREATE TABLE IF NOT EXISTS schema_marker (
    name    TEXT PRIMARY KEY,
    applied INTEGER NOT NULL DEFAULT 0
);

-- ── 1. rebuild `namespace` as nullable ─────────────────────────────────
-- SQLite ≥3.35 supports DROP COLUMN. The NOT NULL → NULL rewrite is
-- cleanest via the documented rename + add + copy + drop dance.
--
-- Drop the legacy index FIRST so SQLite doesn't trip over the
-- rename/drop column dance with an active index pointing at the old
-- name. We re-create the index on the rebuilt column below.

DROP INDEX IF EXISTS idx_morpheus_runs_namespace;

ALTER TABLE morpheus_runs RENAME COLUMN namespace TO namespace_legacy;

ALTER TABLE morpheus_runs ADD COLUMN namespace TEXT;  -- nullable, no default

UPDATE morpheus_runs
   SET namespace = namespace_legacy
 WHERE namespace IS NULL;

ALTER TABLE morpheus_runs DROP COLUMN namespace_legacy;

-- ── 2. add missing columns (all idempotent via DEFAULT 0 / NULL) ────────
-- The migration runner also swallows "duplicate column name" errors as
-- a defense-in-depth measure, so even if the marker check is bypassed
-- these ADD COLUMN statements are idempotent on re-run.

ALTER TABLE morpheus_runs ADD COLUMN phase TEXT;
ALTER TABLE morpheus_runs ADD COLUMN triggered_by TEXT NOT NULL DEFAULT 'cron';

ALTER TABLE morpheus_runs ADD COLUMN window_started_at TEXT;
ALTER TABLE morpheus_runs ADD COLUMN window_ended_at TEXT;
ALTER TABLE morpheus_runs ADD COLUMN window_hours INTEGER NOT NULL DEFAULT 168;
ALTER TABLE morpheus_runs ADD COLUMN cluster_min_size INTEGER NOT NULL DEFAULT 3;

ALTER TABLE morpheus_runs ADD COLUMN memories_scanned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE morpheus_runs ADD COLUMN clusters_found INTEGER NOT NULL DEFAULT 0;
ALTER TABLE morpheus_runs ADD COLUMN summaries_created INTEGER NOT NULL DEFAULT 0;

-- v4.2 consolidate mirror
ALTER TABLE morpheus_runs ADD COLUMN memories_consolidated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE morpheus_runs ADD COLUMN clusters_consolidated INTEGER NOT NULL DEFAULT 0;

-- v4.2 extract mirror
ALTER TABLE morpheus_runs ADD COLUMN triples_extracted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE morpheus_runs ADD COLUMN memories_processed_for_extraction INTEGER NOT NULL DEFAULT 0;

-- ── 3. drop the dead owner_id column ───────────────────────────────────
-- Pre-condition: greps across the whole repo confirm nothing outside
-- this table definition references morpheus_runs.owner_id. Documented
-- in SPECIFICATION.md but never queried or written.

ALTER TABLE morpheus_runs DROP COLUMN owner_id;

-- ── 4. align status default ────────────────────────────────────────────
-- SQLite has no CHECK constraint enforcement on legacy status values,
-- and SQLite doesn't support ``ALTER COLUMN DROP DEFAULT`` at the
-- SQL level — only ``ALTER TABLE ... ADD COLUMN`` with a DEFAULT, and
-- ALTER TABLE RENAME COLUMN are supported. The legacy
-- ``status DEFAULT 'pending'`` would surface a stale default for any
-- INSERT that omits the status column; the ABC's
-- ``SqliteMorpheusRepository.begin_run`` always explicitly writes
-- ``status='running'`` so the bad default never reaches production
-- rows. Leaving the default in place rather than rebuilding the table
-- (the only SQLite-supported way to change a column DEFAULT) keeps
-- the migration safe for the 19-column canonical shape.
--
-- No-op marker so the migration layout stays consistent with the
-- Postgres / Oracle / Db2 parity migrations.
SELECT 1;

-- ── 5. indexes (matching Postgres partial / ordering indexes) ─────────
-- Postgres has:
--   CREATE INDEX idx_morpheus_runs_status    ON morpheus_runs(status);
--   CREATE INDEX idx_morpheus_runs_started   ON morpheus_runs(started_at DESC);
--   CREATE INDEX idx_morpheus_runs_namespace ON morpheus_runs(namespace)
--       WHERE namespace IS NOT NULL;
-- The legacy ``idx_morpheus_runs_namespace`` was dropped in step 1
-- because the rebuild dance (RENAME COLUMN + DROP COLUMN) makes SQLite
-- trip over the index referencing a column that has been renamed out
-- from under it. Re-create the index here on the rebuilt ``namespace``
-- column — we drop the partial WHERE clause since SQLite handles the
-- equality predicate well at the planner level.
CREATE INDEX IF NOT EXISTS idx_morpheus_runs_namespace
    ON morpheus_runs(namespace);

-- ── 6. mark migration applied ─────────────────────────────────────────
-- The Python migration runner reads this marker row on the next open()
-- call and skips re-running this whole file. The key MUST match the
-- migration filename so the lookup in `_apply_migrations` finds it.
-- Done last so a partial body failure leaves the marker unset and the
-- migration will retry on the next open().

INSERT INTO schema_marker (name, applied) VALUES ('migrations_v6_3_morpheus_runs_parity_sqlite.sql', 1);