--#SET TERMINATOR @
-- 0015_journal.sql — Db2 12.1.5 (Oracle Compat) port.
--
-- Canonical journal = the per-owner/per-namespace journal API table
-- (owner_id, namespace, entry_date, topic, content, metadata, deleted_at),
-- matching PostgreSQL and the SQLite/Postgres JournalRepository. Earlier
-- revisions created an audit-shaped journal (entity_id/action/before/after)
-- that no code path ever wrote to (the real change-audit lives in
-- memory_audit_chain / mcp_audit_log), which left the journal API
-- unimplementable. Db2Backend inherits the Oracle journal methods, so the
-- table shape must match.
--
-- The reconcile block mirrors the Oracle sibling: a journal that is
-- unmistakably the dead audit shape (has ENTITY_ID, lacks OWNER_ID) is
-- dropped when empty (it never carried rows) or renamed aside when not, so
-- the canonical CREATE below can take the name without discarding data. The
-- CONTINUE HANDLER on SQLSTATE 42710 makes the CREATE statements idempotent
-- on replay. metadata is a plain CLOB (JSON text), consistent with Oracle.

BEGIN
  FOR c AS
    SELECT COUNT(*) AS has_audit FROM SYSCAT.COLUMNS
     WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'JOURNAL' AND COLNAME = 'ENTITY_ID'
  DO
    IF c.has_audit > 0
       AND NOT EXISTS (SELECT 1 FROM SYSCAT.COLUMNS
                        WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'JOURNAL' AND COLNAME = 'OWNER_ID')
    THEN
      -- Rename aside via dynamic SQL only: a static ``FROM journal`` is validated
      -- at compound-statement COMPILE time and fails with SQL0204N on a fresh DB
      -- where the table does not yet exist. Rename (never DROP) keeps the
      -- no-data-loss guarantee; an empty divergent table lands aside harmlessly.
      EXECUTE IMMEDIATE 'RENAME TABLE journal TO journal_audit_legacy_0015';
    END IF;
  END FOR;
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE journal (
      id VARCHAR(36) NOT NULL,
      owner_id VARCHAR(100) DEFAULT ''default'' NOT NULL,
      namespace VARCHAR(100) DEFAULT ''default'' NOT NULL,
      entry_date DATE NOT NULL,
      topic VARCHAR(100),
      content CLOB(1M),
      metadata CLOB(1M),
      created TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
      deleted_at TIMESTAMP(6),
      CONSTRAINT pk_journal PRIMARY KEY (id)
    )';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_journal_owner_namespace ON journal (owner_id, namespace)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_journal_entry_date ON journal (entry_date DESC)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_journal_topic ON journal (topic)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_journal_created ON journal (created DESC)';
END@
