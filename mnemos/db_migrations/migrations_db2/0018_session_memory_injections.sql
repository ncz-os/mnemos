-- 0018_session_memory_injections.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE session_memory_injections (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    session_id VARCHAR(36) NOT NULL,
    memory_id VARCHAR(36) NOT NULL,
    injected_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
    weight DOUBLE DEFAULT 1.0 NOT NULL
);

CREATE INDEX idx_session_memory_injections_session ON session_memory_injections (session_id);
CREATE INDEX idx_session_memory_injections_memory ON session_memory_injections (memory_id);
