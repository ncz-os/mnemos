-- 0004_oauth_providers.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE oauth_providers (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    client_id VARCHAR(200),
    client_secret_hash VARCHAR(200),
    auth_url VARCHAR(500),
    token_url VARCHAR(500),
    scopes CLOB(1M),
    enabled SMALLINT DEFAULT 1 NOT NULL,
    created TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX idx_oauth_providers_name ON oauth_providers (name);
