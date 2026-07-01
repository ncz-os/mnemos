-- 0046_graeae_soft_delete_ownership.sql — Oracle 23ai parity.
--
-- Mirrors db/migrations/0046_graeae_soft_delete_ownership.sql.
--
-- Backfills the ownership + soft-delete columns the implemented GRAEAE
-- consultation read/write path requires but which 0002_graeae.sql never
-- defined for Oracle:
--   * OracleConsultationsRepository.{list_audit_log,fetch_audit_chain,
--     get_consultation} scope on c.owner_id / c.namespace and filter
--     c.deleted_at IS NULL / al.deleted_at IS NULL.
--   * create_consultation_with_audit INSERTs owner_id + namespace.
--
-- Without these, a fresh Oracle install raises ORA-00904 on the very
-- first consultation write and on every audit-log read. Production was
-- only working because the columns were added out-of-band (deleted_at
-- via the 6.0.1-graeae-oraclereads hotfix; owner_id/namespace earlier).
--
-- Idempotent via a user_tab_columns existence guard, so it is a no-op on
-- any DB that already carries the columns (existing prod is untouched —
-- its current owner_id VARCHAR2(64) / namespace VARCHAR2(256) shapes are
-- preserved; only genuinely fresh installs get the canonical
-- VARCHAR2(100) DEFAULT 'default' shape used elsewhere in the Oracle
-- core schema).

DECLARE
  v_count NUMBER;
  PROCEDURE add_col(p_table VARCHAR2, p_col VARCHAR2, p_ddl VARCHAR2) IS
  BEGIN
    SELECT COUNT(*) INTO v_count
      FROM user_tab_columns
     WHERE table_name = UPPER(p_table)
       AND column_name = UPPER(p_col);
    IF v_count = 0 THEN
      EXECUTE IMMEDIATE 'ALTER TABLE ' || p_table || ' ADD (' || p_ddl || ')';
    END IF;
  END;
BEGIN
  add_col('graeae_consultations', 'owner_id',
          'owner_id VARCHAR2(100) DEFAULT ''default'' NOT NULL');
  add_col('graeae_consultations', 'namespace',
          'namespace VARCHAR2(100) DEFAULT ''default'' NOT NULL');
  add_col('graeae_consultations', 'deleted_at',
          'deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL');
  add_col('graeae_audit_log', 'deleted_at',
          'deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL');
END;
/

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count
    FROM user_indexes
   WHERE index_name = 'IDX_GRAEAE_CONS_OWNER_NS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE
      'CREATE INDEX idx_graeae_cons_owner_ns '
      || 'ON graeae_consultations (owner_id, namespace)';
  END IF;
END;
/
