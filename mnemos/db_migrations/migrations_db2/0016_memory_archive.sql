-- 0016_memory_archive.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE memory_archive (
    id VARCHAR(36) PRIMARY KEY,
    original_memory_id VARCHAR(36) NOT NULL,
    content CLOB(1M) NOT NULL,
    metadata CLOB(1M),
    archived_at TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    reason VARCHAR(100)
);

CREATE INDEX idx_memory_archive_original ON memory_archive (original_memory_id);
