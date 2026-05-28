-- 0012_pantheon_routing_audit.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE pantheon_routing_audit (
    id VARCHAR2(36) PRIMARY KEY,
    consultation_id VARCHAR2(36),
    muse VARCHAR2(100),
    prompt_hash VARCHAR2(128),
    chosen_model VARCHAR2(100),
    routing_reason CLOB,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_pantheon_routing_consult ON pantheon_routing_audit (consultation_id);
