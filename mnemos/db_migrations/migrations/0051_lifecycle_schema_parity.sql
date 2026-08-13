-- Parity restatement for PostgreSQL.
--
-- Db2 and Oracle carry a numbered 0051 that back-fills soft-delete and tenancy
-- columns on already-populated databases. PostgreSQL received the same columns
-- through its normalized schema, so this file only asserts that end state
-- idempotently and is a no-op on a database built from the PG migrations.
--
-- Deliberately NOT mirrored from the Db2 file: its
--   ALTER TABLE memory_archive ALTER COLUMN id SET DATA TYPE VARCHAR(100)
-- widening has no PostgreSQL counterpart -- PG already uses TEXT, which is
-- unbounded, so there is nothing to widen.

ALTER TABLE memory_branches            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE entities                   ADD COLUMN IF NOT EXISTS owner_id   TEXT NOT NULL DEFAULT 'default';
ALTER TABLE entities                   ADD COLUMN IF NOT EXISTS namespace  TEXT NOT NULL DEFAULT 'default';
ALTER TABLE entities                   ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE session_memory_injections  ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
