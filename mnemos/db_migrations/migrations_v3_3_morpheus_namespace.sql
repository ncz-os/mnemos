-- MNEMOS v3.3 — MORPHEUS per-namespace dream scoping (slice 2 follow-up)
--
-- Adds optional namespace filter to morpheus_runs so a dream can be
-- scoped to a single tenant's memories instead of the global corpus.
-- NULL means "all namespaces" (current behavior, the default for
-- existing rows so the migration is backwards-compatible).
--
-- The replay/cluster phases honour this column when set; rollback
-- continues to work via morpheus_run_id (no per-namespace filter
-- needed — every memory inserted by a run is tagged regardless).
--
-- Idempotent: every ALTER / CREATE uses IF NOT EXISTS.

ALTER TABLE morpheus_runs
    ADD COLUMN IF NOT EXISTS namespace text;

CREATE INDEX IF NOT EXISTS idx_morpheus_runs_namespace
    ON morpheus_runs(namespace)
    WHERE namespace IS NOT NULL;
