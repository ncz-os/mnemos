-- 0006_oauth_sessions.sql — Oracle 23ai/26ai port for MNEMOS parity.
--
-- Canonical oauth_sessions matches PostgreSQL (db/migrations_v3_oauth.sql):
-- DB-backed, revocable session store keyed by session_id, with user_id,
-- identity_id, expires_at, last_used_at, revoked flag, user_agent, ip_address,
-- revoked_at. The OracleOAuthRepository methods (create_session, revoke_session,
-- revoke_all_sessions, get_identity_for_session) already reference these.
--
-- Earlier revisions created a divergent shape (id PK, access_token_hash, no
-- session_id/user_id/revoked). The guard reconciles ONLY that dead divergent
-- table (exists, lacks SESSION_ID): drops it when empty, renames it aside when
-- not (no data loss). A canonical table (has SESSION_ID) is never touched. Safe
-- to replay.

DECLARE
    v_tbl  NUMBER;
    v_new  NUMBER;
    v_rows NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_tbl
      FROM user_tables WHERE table_name = 'OAUTH_SESSIONS';
    SELECT COUNT(*) INTO v_new
      FROM user_tab_columns
     WHERE table_name = 'OAUTH_SESSIONS' AND column_name = 'SESSION_ID';
    IF v_tbl > 0 AND v_new = 0 THEN
        EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM oauth_sessions' INTO v_rows;
        IF v_rows = 0 THEN
            EXECUTE IMMEDIATE 'DROP TABLE oauth_sessions CASCADE CONSTRAINTS';
        ELSE
            EXECUTE IMMEDIATE 'RENAME oauth_sessions TO oauth_sessions_legacy_0006';
        END IF;
    END IF;
END;
/

CREATE TABLE IF NOT EXISTS oauth_sessions (
    session_id VARCHAR2(200) PRIMARY KEY,
    user_id VARCHAR2(100) NOT NULL,
    identity_id VARCHAR2(36),
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_used_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    revoked NUMBER(1) DEFAULT 0 NOT NULL,
    user_agent VARCHAR2(1000),
    ip_address VARCHAR2(50),
    revoked_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_oauth_sessions_user ON oauth_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_sessions_identity ON oauth_sessions (identity_id);
CREATE INDEX IF NOT EXISTS idx_oauth_sessions_expires ON oauth_sessions (expires_at);
