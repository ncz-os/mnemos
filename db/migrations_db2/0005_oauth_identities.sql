-- 0005_oauth_identities.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE oauth_identities (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    provider_id VARCHAR(36) NOT NULL,
    external_id VARCHAR(200) NOT NULL,
    profile CLOB(1M),
    created TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_login_at TIMESTAMP(6) WITH TIME ZONE,
    CONSTRAINT uq_oauth_identity UNIQUE (provider_id, external_id)
);

CREATE INDEX idx_oauth_identities_user ON oauth_identities (user_id);
CREATE INDEX idx_oauth_identities_provider ON oauth_identities (provider_id);
