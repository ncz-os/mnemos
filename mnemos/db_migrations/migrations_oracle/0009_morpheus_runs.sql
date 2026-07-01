-- 0009_morpheus_runs.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE morpheus_runs (
    id VARCHAR2(36) PRIMARY KEY,
    run_type VARCHAR2(50) NOT NULL,
    status VARCHAR2(20) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    metrics CLOB CHECK (metrics IS JSON),
    error CLOB
);

CREATE INDEX idx_morpheus_runs_status ON morpheus_runs (status);
CREATE INDEX idx_morpheus_runs_started ON morpheus_runs (started_at);
