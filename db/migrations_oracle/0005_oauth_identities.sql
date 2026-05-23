-- 0005_oauth_identities.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE oauth_identities (
    id VARCHAR2(36) PRIMARY KEY,
    user_id VARCHAR2(36) NOT NULL,
    provider_id VARCHAR2(36) NOT NULL,
    external_id VARCHAR2(200) NOT NULL,
    profile CLOB CHECK (profile IS JSON),
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    last_login_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_oauth_identity UNIQUE (provider_id, external_id)
);

CREATE INDEX idx_oauth_identities_user ON oauth_identities (user_id);
CREATE INDEX idx_oauth_identities_provider ON oauth_identities (provider_id);
