-- MNEMOS v3.3 — MORPHEUS dream-state subsystem (slice 1: foundation)
--
-- MORPHEUS is the off-peak background worker that processes accumulated
-- memory into shaped form: clustering recent memories, synthesising
-- summary memories, and (in later slices) consolidating duplicates and
-- archiving cold material. Per the GRAEAE architecture consensus
-- (consultation 2026-04-25): v1 ships SYNTHESISE only — append-only,
-- zero corpus risk. CONSOLIDATE / ARCHIVE / EXTRACT carry mutation
-- risk and land in v3.4+.
--
-- The audit shape is per-row tagging via morpheus_run_id, NOT a
-- per-dream transaction. A long DB transaction across slow LLM phases
-- would lock-thrash the corpus. Instead every change tags the run id;
-- rollback is a deterministic DELETE WHERE morpheus_run_id = X.
--
-- Idempotent: every CREATE / ALTER uses IF NOT EXISTS. Safe to re-run.

CREATE TABLE IF NOT EXISTS morpheus_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    status              text        NOT NULL DEFAULT 'running',
    phase               text,
    triggered_by        text        NOT NULL DEFAULT 'cron',

    -- Window the dream operated over
    window_started_at   timestamptz,
    window_ended_at     timestamptz,
    window_hours        int         NOT NULL DEFAULT 168,
    cluster_min_size    int         NOT NULL DEFAULT 3,

    -- Outputs (filled as phases complete)
    memories_scanned    int         NOT NULL DEFAULT 0,
    clusters_found      int         NOT NULL DEFAULT 0,
    summaries_created   int         NOT NULL DEFAULT 0,

    -- Failure mode capture
    error               text,
    config              jsonb       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT morpheus_runs_status_check
        CHECK (status IN ('running','success','failed','rolled_back')),
    CONSTRAINT morpheus_runs_triggered_by_check
        CHECK (triggered_by IN ('cron','manual','api'))
);

CREATE INDEX IF NOT EXISTS idx_morpheus_runs_status    ON morpheus_runs(status);
CREATE INDEX IF NOT EXISTS idx_morpheus_runs_started   ON morpheus_runs(started_at DESC);


-- Tag memories with the run that created (v1) or modified (v2+) them.
-- Indexed PARTIAL — most memories never have a run id, so the partial
-- index keeps it tiny and the rollback DELETE blazingly fast.
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS morpheus_run_id uuid
    REFERENCES morpheus_runs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_memories_morpheus_run
    ON memories(morpheus_run_id)
    WHERE morpheus_run_id IS NOT NULL;


-- Synthesised summaries link back to the originals that informed them.
-- text[] of memory ids — pgvector / pg supports gin indexing for
-- contains/overlaps queries ("which summaries reference mem_xyz?").
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS source_memories text[];

CREATE INDEX IF NOT EXISTS idx_memories_source_memories
    ON memories USING gin(source_memories)
    WHERE source_memories IS NOT NULL;


-- Provenance string: 'morpheus_local' for synthesised summaries,
-- federation_source already exists for cross-instance imports.
-- Distinct columns because a memory can be morpheus_local AND have
-- federation_source set if it gets pulled to a peer (peers receive it
-- as someone else's dream, do not re-dream it locally).
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS provenance text;

CREATE INDEX IF NOT EXISTS idx_memories_provenance
    ON memories(provenance)
    WHERE provenance IS NOT NULL;
