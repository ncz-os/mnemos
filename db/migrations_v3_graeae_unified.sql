-- v3.0.0 Unified GRAEAE + MNEMOS Service
-- Adds consultation_memory_refs table to link consultations to injected memories
-- Enables auditable memory injection tracking for reasoning workflows

BEGIN;

-- 1. consultation_memory_refs: Track which memories were injected into each consultation
-- Links GRAEAE consultations to the memories they referenced
-- Enables citation tracking and memory provenance analysis

CREATE TABLE IF NOT EXISTS consultation_memory_refs (
    consultation_id UUID      NOT NULL,
    memory_id       TEXT      NOT NULL,
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
