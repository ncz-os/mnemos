-- 0020_compression_quality_log.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE compression_quality_log (
    id VARCHAR2(36) PRIMARY KEY,
    memory_id VARCHAR2(36) NOT NULL,
    original_size NUMBER,
    compressed_size NUMBER,
    quality_rating NUMBER,
    quality_summary CLOB,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_compression_quality_memory ON compression_quality_log (memory_id);
CREATE INDEX idx_compression_quality_created ON compression_quality_log (created);
