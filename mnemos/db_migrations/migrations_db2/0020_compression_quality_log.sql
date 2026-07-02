-- 0020_compression_quality_log.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE compression_quality_log (
    id VARCHAR(36) PRIMARY KEY,
    memory_id VARCHAR(36) NOT NULL,
    original_size BIGINT,
    compressed_size BIGINT,
    quality_rating DOUBLE,
    quality_summary CLOB(1M),
    created TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX idx_compression_quality_memory ON compression_quality_log (memory_id);
CREATE INDEX idx_compression_quality_created ON compression_quality_log (created);
