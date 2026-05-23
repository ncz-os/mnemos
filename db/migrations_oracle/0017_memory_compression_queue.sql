-- 0017_memory_compression_queue.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE memory_compression_queue (
    id VARCHAR2(36) PRIMARY KEY,
    memory_id VARCHAR2(36) NOT NULL,
    priority NUMBER DEFAULT 0 NOT NULL,
    queued_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR2(20) DEFAULT 'pending' NOT NULL
);

CREATE INDEX idx_memory_compression_queue_status ON memory_compression_queue (status);
CREATE INDEX idx_memory_compression_queue_priority ON memory_compression_queue (priority);
