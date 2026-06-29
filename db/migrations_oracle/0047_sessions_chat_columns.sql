-- 0047_sessions_chat_columns.sql — Oracle 23ai/26ai port for MNEMOS parity.
--
-- The chat SessionsRepository (create_session, get_session, add_message)
-- expects the PostgreSQL v2_sessions shape: sessions carry namespace/model/
-- last_activity/message_count/total_tokens/compression_tier/deleted_at, and
-- session_messages carry message_id/model/tokens_used/memories_injected/
-- deleted_at. The Oracle 0001 core schema only created the minimal protocol
-- columns; this migration adds the chat columns additively (existing protocol
-- columns from 0001/0038 are untouched). Idempotent per-column guards make it
-- safe to replay. Mirrors PostgreSQL, where these columns ship in the named
-- migrations_v2_sessions.sql (db/migrations/0047 is a no-op parity anchor).

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
    add_col('sessions', 'namespace',        'namespace VARCHAR2(100) DEFAULT ''default'' NOT NULL');
    add_col('sessions', 'model',            'model VARCHAR2(200)');
    add_col('sessions', 'last_activity',    'last_activity TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP');
    add_col('sessions', 'message_count',    'message_count NUMBER DEFAULT 0 NOT NULL');
    add_col('sessions', 'total_tokens',     'total_tokens NUMBER DEFAULT 0 NOT NULL');
    add_col('sessions', 'compression_tier', 'compression_tier NUMBER DEFAULT 1 NOT NULL');
    add_col('sessions', 'deleted_at',       'deleted_at TIMESTAMP WITH TIME ZONE');

    add_col('session_messages', 'message_id',        'message_id VARCHAR2(36)');
    add_col('session_messages', 'model',             'model VARCHAR2(200)');
    add_col('session_messages', 'tokens_used',       'tokens_used NUMBER');
    add_col('session_messages', 'memories_injected', 'memories_injected NUMBER DEFAULT 0');
    add_col('session_messages', 'deleted_at',        'deleted_at TIMESTAMP WITH TIME ZONE');
END;
/

CREATE INDEX IF NOT EXISTS idx_sessions_user_namespace ON sessions (user_id, namespace);
