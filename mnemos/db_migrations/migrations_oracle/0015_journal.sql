-- 0015_journal.sql — Oracle 23ai/26ai port for MNEMOS parity.
--
-- Canonical journal = the per-owner/per-namespace journal API table
-- (owner_id, namespace, entry_date, topic, content, metadata, deleted_at),
-- matching PostgreSQL (db/migrations.sql `journal` + v3_ownership owner_id
-- + v3_5_state_journal_namespace namespace + v4_2 soft-delete deleted_at)
-- and the SQLite/Postgres JournalRepository implementations.
--
-- Earlier revisions of this file created an audit-shaped journal
-- (entity_id/action/before/after) that NO code path ever wrote to — the
-- real change-audit lives in memory_audit_chain / mcp_audit_log /
-- pantheon_routing_audit. That divergent shape left the journal API
-- (OracleJournalRepository.create_journal_entry/...) unimplementable.
-- This migration reconciles to the canonical shape.
--
-- The PL/SQL guard reconciles ONLY a journal table that is unmistakably the
-- dead audit shape (has ENTITY_ID, lacks OWNER_ID); a canonical journal
-- (has OWNER_ID) is never touched. To avoid any data-loss risk it drops the
-- audit table only when empty (it never carried rows in practice); a
-- non-empty one is renamed aside instead, freeing the name for the canonical
-- CREATE without discarding rows. Safe to replay on any environment.
--
-- metadata is a plain CLOB (no IS JSON constraint): python-oracledb
-- auto-decodes an IS JSON column into a dict, whereas Postgres/SQLite/Db2
-- return JSON text — selecting a plain CLOB keeps the journal API response
-- a JSON string across all backends. (Db2Backend inherits these journal
-- methods, so the SQL stays Db2-translatable — no JSON_SERIALIZE.)

DECLARE
    v_audit NUMBER;
    v_api   NUMBER;
    v_rows  NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_audit
      FROM user_tab_columns
     WHERE table_name = 'JOURNAL' AND column_name = 'ENTITY_ID';
    SELECT COUNT(*) INTO v_api
      FROM user_tab_columns
     WHERE table_name = 'JOURNAL' AND column_name = 'OWNER_ID';
    IF v_audit > 0 AND v_api = 0 THEN
        EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM journal' INTO v_rows;
        IF v_rows = 0 THEN
            EXECUTE IMMEDIATE 'DROP TABLE journal CASCADE CONSTRAINTS';
        ELSE
            EXECUTE IMMEDIATE 'RENAME journal TO journal_audit_legacy_0015';
        END IF;
    END IF;
END;
/

CREATE TABLE IF NOT EXISTS journal (
    id VARCHAR2(36) PRIMARY KEY,
    owner_id VARCHAR2(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR2(100) DEFAULT 'default' NOT NULL,
    entry_date DATE NOT NULL,
    topic VARCHAR2(100),
    content CLOB,
    metadata CLOB,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_journal_owner_namespace ON journal (owner_id, namespace);
CREATE INDEX IF NOT EXISTS idx_journal_entry_date ON journal (entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_journal_topic ON journal (topic);
CREATE INDEX IF NOT EXISTS idx_journal_created ON journal (created DESC);
