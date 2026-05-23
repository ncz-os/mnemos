-- 0018_session_memory_injections.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE session_memory_injections (
    id VARCHAR2(36) PRIMARY KEY,
    session_id VARCHAR2(36) NOT NULL,
    memory_id VARCHAR2(36) NOT NULL,
    injected_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    weight NUMBER DEFAULT 1.0 NOT NULL
);

CREATE INDEX idx_session_memory_injections_session ON session_memory_injections (session_id);
CREATE INDEX idx_session_memory_injections_memory ON session_memory_injections (memory_id);
