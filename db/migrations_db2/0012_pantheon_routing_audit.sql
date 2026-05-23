-- 0012_pantheon_routing_audit.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE pantheon_routing_audit (
    id VARCHAR(36) PRIMARY KEY,
    consultation_id VARCHAR(36),
    muse VARCHAR(100),
    prompt_hash VARCHAR(128),
    chosen_model VARCHAR(100),
    routing_reason CLOB(1M),
    created TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX idx_pantheon_routing_consult ON pantheon_routing_audit (consultation_id);
