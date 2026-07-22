-- 0048_memory_versions_group_id.sql — Oracle 23ai port for MNEMOS parity (GitLab #2).
--
-- Backfill the memory_versions table with a ``group_id`` column so the
-- Oracle version_visibility predicate (mirroring the live-memory
-- `mnemos_acl_select` / group_read widening) can fire against historical
-- snapshots. Memory_acl is keyed on memory_id (not on snapshot id), so
-- the ACL widening does NOT require a schema change to memory_versions
-- on the Oracle side either — only the group branch does.
--
-- Mirrors mnemos/db_migrations/migrations/0048_memory_versions_group_id.sql.
-- Idempotent via the ORA-00955 / ORA-01430 / column-already-exists handlers
-- used elsewhere in the Oracle migration baseline.

-- 1. Idempotent ADD COLUMN for group_id.
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tab_columns
     WHERE table_name = 'MEMORY_VERSIONS' AND column_name = 'GROUP_ID';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE memory_versions ADD (group_id VARCHAR2(100))';
    END IF;
END;
/

-- 2. Backfill existing snapshot rows from the live memories table.
-- Same rationale as the Postgres migration: we use the live memory's
-- CURRENT group_id because historical group_id at snapshot time is not
-- recoverable from the existing schema. Documented in KNOWN_LIMITATIONS.
UPDATE memory_versions mv
   SET mv.group_id = (
       SELECT m.group_id FROM memories m WHERE m.id = mv.memory_id
   )
 WHERE mv.group_id IS NULL
   AND EXISTS (SELECT 1 FROM memories m WHERE m.id = mv.memory_id AND m.group_id IS NOT NULL);
COMMIT;

-- 3. Idempotent CREATE INDEX for the (memory_id, group_id) predicate.
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_indexes
     WHERE index_name = 'IDX_MV_MEMORY_ID_GROUP_ID';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'CREATE INDEX idx_mv_memory_id_group_id ON memory_versions (memory_id, group_id)';
    END IF;
END;
/
