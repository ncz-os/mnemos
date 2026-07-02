--#SET TERMINATOR @
-- 0006_oauth_sessions.sql — Db2 12.1.5 (Oracle Compat) port.
--
-- Canonical oauth_sessions matches PostgreSQL: DB-backed revocable session
-- store keyed by session_id (user_id, identity_id, expires_at, last_used_at,
-- revoked, user_agent, ip_address, revoked_at). Db2Backend inherits the Oracle
-- OAuth methods, so the shape must match. Earlier revisions created a divergent
-- shape (id PK, access_token_hash, no session_id). The reconcile block mirrors
-- the Oracle sibling: the dead divergent table (exists, lacks SESSION_ID) is
-- dropped when empty or renamed aside when not; a canonical table is untouched.

BEGIN
  FOR c AS
    SELECT COUNT(*) AS has_tbl FROM SYSCAT.TABLES
     WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'OAUTH_SESSIONS'
  DO
    IF c.has_tbl > 0
       AND NOT EXISTS (SELECT 1 FROM SYSCAT.COLUMNS
                        WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'OAUTH_SESSIONS' AND COLNAME = 'SESSION_ID')
    THEN
      -- Rename aside via dynamic SQL only: a static ``FROM oauth_sessions`` is
      -- validated at compound-statement COMPILE time and fails with SQL0204N on
      -- a fresh DB where the table does not yet exist. Rename (never DROP) keeps
      -- the no-data-loss guarantee; an empty divergent table lands aside harmlessly.
      EXECUTE IMMEDIATE 'RENAME TABLE oauth_sessions TO oauth_sessions_legacy_0006';
    END IF;
  END FOR;
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE oauth_sessions (
      session_id VARCHAR(200) NOT NULL,
      user_id VARCHAR(100) NOT NULL,
      identity_id VARCHAR(36),
      created TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
      expires_at TIMESTAMP(6) NOT NULL,
      last_used_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
      revoked SMALLINT DEFAULT 0 NOT NULL,
      user_agent VARCHAR(1000),
      ip_address VARCHAR(50),
      revoked_at TIMESTAMP(6),
      CONSTRAINT pk_oauth_sessions PRIMARY KEY (session_id)
    )';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_oauth_sessions_user ON oauth_sessions (user_id)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_oauth_sessions_identity ON oauth_sessions (identity_id)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_oauth_sessions_expires ON oauth_sessions (expires_at)';
END@
