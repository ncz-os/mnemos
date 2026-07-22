--#SET TERMINATOR @
-- 0048_memory_versions_group_id.sql — Db2 12.1.5 (Oracle Compat) port.
--
-- Backfill the memory_versions table with a ``group_id`` column so the
-- Db2 version_visibility predicate (mirroring the live-memory
-- `mnemos_acl_select` / group_read widening) can fire against historical
-- snapshots. Memory_acl is keyed on memory_id (not on snapshot id), so
-- the ACL widening does NOT require a schema change to memory_versions
-- on the Db2 side either — only the group branch does.
--
-- Mirrors mnemos/db_migrations/migrations/0048_memory_versions_group_id.sql.

-- Idempotent ALTER TABLE ADD COLUMN. SQLSTATE '42711' = column already
-- exists; SQLSTATE '42710' is the inverse of '42704' for some envs.
BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42704' BEGIN END;
    EXECUTE IMMEDIATE 'ALTER TABLE memory_versions ADD COLUMN group_id VARCHAR(100)';
END@

-- Backfill existing snapshot rows from the live memories table. Same
-- rationale as Postgres: we use the live memory's CURRENT group_id
-- because historical group_id is not recoverable. Documented in
-- KNOWN_LIMITATIONS.
UPDATE memory_versions mv
   SET group_id = (
       SELECT m.group_id FROM memories m WHERE m.id = mv.memory_id
   )
 WHERE mv.group_id IS NULL
   AND EXISTS (SELECT 1 FROM memories m WHERE m.id = mv.memory_id AND m.group_id IS NOT NULL)@

-- Idempotent CREATE INDEX for the (memory_id, group_id) predicate.
BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
    EXECUTE IMMEDIATE 'CREATE INDEX idx_mv_memory_id_group_id ON memory_versions (memory_id, group_id)';
END@
