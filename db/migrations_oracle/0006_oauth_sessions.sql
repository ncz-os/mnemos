-- 0006_oauth_sessions.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE oauth_sessions (
    id VARCHAR2(36) PRIMARY KEY,
    identity_id VARCHAR2(36) NOT NULL,
    access_token_hash VARCHAR2(200),
    refresh_token_hash VARCHAR2(200),
    expires_at TIMESTAMP WITH TIME ZONE,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_oauth_sessions_identity ON oauth_sessions (identity_id);
