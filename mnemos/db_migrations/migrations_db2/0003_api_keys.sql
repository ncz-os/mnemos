-- 0003_api_keys.sql — Db2 12.1.5 (Oracle Compat) port for MNEMOS parity.
-- Adapted from Oracle 23ai version with Db2 timestamp / CLOB handling.

CREATE TABLE api_keys (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    provider VARCHAR(50) NOT NULL,
    key_hash VARCHAR(128) NOT NULL,
    scopes CLOB(1M),
    rate_limit_per_min INTEGER,
    created TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_used_at TIMESTAMP(6) WITH TIME ZONE,
    revoked_at TIMESTAMP(6) WITH TIME ZONE,
    owner_id VARCHAR(36),
    namespace VARCHAR(100)
);

CREATE INDEX idx_api_keys_provider ON api_keys (provider);
CREATE INDEX idx_api_keys_owner ON api_keys (owner_id);
CREATE INDEX idx_api_keys_namespace ON api_keys (namespace);
