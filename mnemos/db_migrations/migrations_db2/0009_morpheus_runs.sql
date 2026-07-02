-- 0009_morpheus_runs.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE morpheus_runs (
    id VARCHAR(36) PRIMARY KEY,
    run_type VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL,
    started_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
    finished_at TIMESTAMP(6),
    metrics CLOB(1M),
    error CLOB(1M)
);

CREATE INDEX idx_morpheus_runs_status ON morpheus_runs (status);
CREATE INDEX idx_morpheus_runs_started ON morpheus_runs (started_at);
