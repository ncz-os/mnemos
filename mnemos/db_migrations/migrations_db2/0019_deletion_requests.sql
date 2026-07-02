-- 0019_deletion_requests.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE deletion_requests (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    memory_id VARCHAR(36) NOT NULL,
    requested_by VARCHAR(36),
    reason CLOB(1M),
    requested_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
    processed_at TIMESTAMP(6),
    status VARCHAR(20) DEFAULT 'pending' NOT NULL
);

CREATE INDEX idx_deletion_requests_memory ON deletion_requests (memory_id);
CREATE INDEX idx_deletion_requests_status ON deletion_requests (status);
