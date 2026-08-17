-- Lifecycle-worker schema parity for already-populated Oracle databases.
-- The anonymous block makes nullable GDPR columns replay-safe; ID widening is
-- non-destructive and matches the canonical memories.id width.

DECLARE
    v_count NUMBER;
    PROCEDURE add_col(p_table VARCHAR2, p_col VARCHAR2, p_ddl VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count
          FROM user_tab_columns
         WHERE table_name = UPPER(p_table) AND column_name = UPPER(p_col);
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE ' || p_table || ' ADD (' || p_ddl || ')';
        END IF;
    END;
BEGIN
    add_col('memory_branches', 'deleted_at', 'deleted_at TIMESTAMP WITH TIME ZONE');
    add_col('entities', 'owner_id', 'owner_id VARCHAR2(256) DEFAULT ''default'' NOT NULL');
    add_col('entities', 'namespace', 'namespace VARCHAR2(256) DEFAULT ''default'' NOT NULL');
    add_col('entities', 'deleted_at', 'deleted_at TIMESTAMP WITH TIME ZONE');
    add_col('session_memory_injections', 'deleted_at', 'deleted_at TIMESTAMP WITH TIME ZONE');
END;
/

ALTER TABLE memory_archive MODIFY (id VARCHAR2(100));
ALTER TABLE memory_archive MODIFY (original_memory_id VARCHAR2(100));
