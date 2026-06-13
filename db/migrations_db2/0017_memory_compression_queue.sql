-- 0017_memory_compression_queue.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE memory_compression_queue (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    memory_id VARCHAR(36) NOT NULL,
    priority INTEGER DEFAULT 0 NOT NULL,
    queued_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(20) DEFAULT 'pending' NOT NULL
);

CREATE INDEX idx_memory_compression_queue_status ON memory_compression_queue (status);
CREATE INDEX idx_memory_compression_queue_priority ON memory_compression_queue (priority);
