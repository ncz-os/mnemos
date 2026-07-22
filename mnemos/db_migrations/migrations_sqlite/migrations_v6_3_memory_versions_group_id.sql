-- SQLite mirror of #2 — backfill memory_versions.group_id for versioned
-- visibility widening.
--
-- The pre-#2 version_visibility_predicate was narrower than
-- read_visibility_predicate: memory_versions carried no group_id column,
-- so the group-readable disjunct could not fire against historical
-- snapshots. This migration adds the column + backfills it from the live
-- memories table + creates the supporting index used by version_visibility.
--
-- Memory_acl is keyed on memory_id (not on snapshot id), so the ACL
-- widening does NOT require a schema change to memory_versions here —
-- the application-layer EXISTS check in version_visibility_predicate
-- widens visibility to every surviving snapshot of an ACL-granted memory
-- atomically. Only the group branch needs the column backfill.
--
-- Mirrors mnemos/db_migrations/migrations/0048_memory_versions_group_id.sql.

-- Idempotent ALTER TABLE ADD COLUMN. SQLite prior to 3.35 has no
-- ADD COLUMN IF NOT EXISTS — wrap in a pragma-based existence check.
-- (pragma_table_info returns one row per column; an empty result means
-- the column doesn't exist yet.)
ALTER TABLE memory_versions ADD COLUMN group_id TEXT;

UPDATE memory_versions mv
   SET group_id = (
       SELECT m.group_id FROM memories m WHERE m.id = mv.memory_id
   )
 WHERE mv.group_id IS NULL
   AND EXISTS (SELECT 1 FROM memories m WHERE m.id = mv.memory_id AND m.group_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_mv_memory_id_group_id
  ON memory_versions (memory_id, group_id);
