-- 0012_pantheon_routing_audit.sql — PostgreSQL parity backfill.
-- Mirrors db/migrations_v4_2_pantheon_routing_audit.sql in the numbered tree.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS pantheon_routing_audit (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id     TEXT,
    tenant_user_id TEXT,
    alias_or_model TEXT,
    resolved_to    TEXT,
    outcome        TEXT,
    latency_ms     INT,
    tokens_in      INT,
    tokens_out     INT,
    cost_usd       NUMERIC(10,4),
    error_class    TEXT,
    payload        JSONB NOT NULL,
    created        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pantheon_routing_audit_created_desc
    ON pantheon_routing_audit (created DESC);

CREATE INDEX IF NOT EXISTS idx_pantheon_routing_audit_tenant_created_desc
    ON pantheon_routing_audit (tenant_user_id, created DESC);
