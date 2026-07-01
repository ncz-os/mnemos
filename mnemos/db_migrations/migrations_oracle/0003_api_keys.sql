-- 0003_api_keys.sql — Oracle 23ai port of api_keys table for MNEMOS parity.
-- Source: PG db/migrations (base schema or early migration)
-- Target: PYTHIA Oracle 23ai (ORCLPDB1)

CREATE TABLE api_keys (
    id VARCHAR2(36) PRIMARY KEY,
    name VARCHAR2(100) NOT NULL,
    provider VARCHAR2(50) NOT NULL,
    key_hash VARCHAR2(128) NOT NULL,
    scopes CLOB CHECK (scopes IS JSON),
    rate_limit_per_min NUMBER,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    last_used_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE,
    owner_id VARCHAR2(36),
    namespace VARCHAR2(100)
);

CREATE INDEX idx_api_keys_provider ON api_keys (provider);
CREATE INDEX idx_api_keys_owner ON api_keys (owner_id);
CREATE INDEX idx_api_keys_namespace ON api_keys (namespace);
