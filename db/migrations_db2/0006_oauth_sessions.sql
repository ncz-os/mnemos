-- 0006_oauth_sessions.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE oauth_sessions (
    id VARCHAR(36) PRIMARY KEY,
    identity_id VARCHAR(36) NOT NULL,
    access_token_hash VARCHAR(200),
    refresh_token_hash VARCHAR(200),
    expires_at TIMESTAMP(6) WITH TIME ZONE,
    created TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP(6) WITH TIME ZONE
);

CREATE INDEX idx_oauth_sessions_identity ON oauth_sessions (identity_id);
