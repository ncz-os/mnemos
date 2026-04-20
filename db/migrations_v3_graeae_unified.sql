-- v3.0.0 Unified GRAEAE + MNEMOS Service
-- Adds consultation_memory_refs and graeae_audit_log tables for consultation tracking
-- Enables auditable memory injection tracking and hash-chained audit logging

BEGIN;

-- 0. graeae_audit_log: Hash-chained audit trail for all GRAEAE consultations (EMIR Article 57)
-- Each entry is cryptographically linked to the previous entry via SHA256(prev_hash + current_data)
-- Prevents tampering and provides a tamper-evident log of reasoning operations

CREATE TABLE IF NOT EXISTS graeae_audit_log (
    id             UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id UUID         NOT NULL,
    prompt         TEXT          NOT NULL,
    provider       VARCHAR(50)   NOT NULL,
    model          VARCHAR(100)  NOT NULL,
    response_text  TEXT,
    response_hash  VARCHAR(64)   NOT NULL,  -- SHA256(response_text)
    chain_hash     VARCHAR(64)   NOT NULL,  -- SHA256(prev_hash + prompt_hash + response_hash)
    prev_chain_hash VARCHAR(64),            -- Points to previous entry
    latency_ms     INT,
    cost_usd       FLOAT,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_audit_log_consultation
        FOREIGN KEY (consultation_id) REFERENCES graeae_consultations(id) ON DELETE CASCADE,
    CONSTRAINT valid_chain_hash CHECK (length(chain_hash) = 64),
    CONSTRAINT valid_response_hash CHECK (length(response_hash) = 64)
);

CREATE INDEX idx_graeae_audit_log_consultation ON graeae_audit_log(consultation_id);
CREATE INDEX idx_graeae_audit_log_created_at ON graeae_audit_log(created_at DESC);
CREATE INDEX idx_graeae_audit_log_chain_hash ON graeae_audit_log(chain_hash);

-- 1. consultation_memory_refs: Track which memories were injected into each consultation
-- Links GRAEAE consultations to the memories they referenced
-- Enables citation tracking and memory provenance analysis

CREATE TABLE IF NOT EXISTS consultation_memory_refs (
    consultation_id UUID      NOT NULL,
    memory_id       TEXT,      -- Allow NULL for soft-delete of referenced memories
    relevance_score FLOAT     DEFAULT NULL,
    injected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (consultation_id, memory_id),
    CONSTRAINT fk_consultation_memory_refs_consultation
        FOREIGN KEY (consultation_id) REFERENCES graeae_consultations(id) ON DELETE CASCADE,
    CONSTRAINT fk_consultation_memory_refs_memory
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE SET NULL
);

-- Index for fast lookup of memories referenced by a consultation
CREATE INDEX idx_consultation_memory_refs_consultation
    ON consultation_memory_refs(consultation_id);

-- Index for fast lookup of which consultations used a specific memory
CREATE INDEX idx_consultation_memory_refs_memory
    ON consultation_memory_refs(memory_id);

-- Index for temporal queries (which memories were injected when)
CREATE INDEX idx_consultation_memory_refs_injected_at
    ON consultation_memory_refs(injected_at DESC);

COMMIT;
