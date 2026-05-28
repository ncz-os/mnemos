-- 0010_morpheus_extract_run_memories.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE morpheus_extract_run_memories (
    run_id VARCHAR2(36) NOT NULL,
    memory_id VARCHAR2(36) NOT NULL,
    extracted_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    PRIMARY KEY (run_id, memory_id)
);

CREATE INDEX idx_morpheus_extract_run ON morpheus_extract_run_memories (run_id);
