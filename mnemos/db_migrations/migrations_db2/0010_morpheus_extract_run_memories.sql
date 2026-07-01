-- 0010_morpheus_extract_run_memories.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE morpheus_extract_run_memories (
    run_id VARCHAR(36) NOT NULL,
    memory_id VARCHAR(36) NOT NULL,
    extracted_at TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (run_id, memory_id)
);

CREATE INDEX idx_morpheus_extract_run ON morpheus_extract_run_memories (run_id);
