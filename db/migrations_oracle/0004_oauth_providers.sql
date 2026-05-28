-- 0004_oauth_providers.sql — Oracle 23ai port for MNEMOS parity.
-- Source: PG db/migrations (oauth_providers table)

CREATE TABLE oauth_providers (
    id VARCHAR2(36) PRIMARY KEY,
    name VARCHAR2(100) NOT NULL UNIQUE,
    client_id VARCHAR2(200),
    client_secret_hash VARCHAR2(200),
    auth_url VARCHAR2(500),
    token_url VARCHAR2(500),
    scopes CLOB CHECK (scopes IS JSON),
    enabled NUMBER(1) DEFAULT 1 NOT NULL,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_oauth_providers_name ON oauth_providers (name);
