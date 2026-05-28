-- 0019_deletion_requests.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE deletion_requests (
    id VARCHAR2(36) PRIMARY KEY,
    memory_id VARCHAR2(36) NOT NULL,
    requested_by VARCHAR2(36),
    reason CLOB,
    requested_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR2(20) DEFAULT 'pending' NOT NULL
);

CREATE INDEX idx_deletion_requests_memory ON deletion_requests (memory_id);
CREATE INDEX idx_deletion_requests_status ON deletion_requests (status);
