-- 0016_memory_archive.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE memory_archive (
    id VARCHAR2(36) PRIMARY KEY,
    original_memory_id VARCHAR2(36) NOT NULL,
    content CLOB NOT NULL,
    metadata CLOB CHECK (metadata IS JSON),
    archived_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    reason VARCHAR2(100)
);

CREATE INDEX idx_memory_archive_original ON memory_archive (original_memory_id);
