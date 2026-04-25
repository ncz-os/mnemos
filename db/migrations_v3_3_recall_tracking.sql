-- MNEMOS v3.3 — Recall-frequency tracking on memories (slice 2 follow-up)
--
-- Adds two columns that capture how often (and when most recently) each
-- memory has been returned by /v1/memories/search. Provides the signal
-- MORPHEUS / PERSEPHONE will use later to decide which memories deserve
-- consolidation vs. archival.
--
-- The third column called out in the audit log — `unique_queries` —
-- was deliberately deferred. Storing the last N unique query strings
-- as a text[] requires either an in-SQL cap-at-N circular-buffer
-- (clumsy with empty-array NULL semantics in postgres) OR a separate
-- recall-log table (an INSERT per memory per search-hit on a hot
-- path). Neither is the right shape for v3.3. Revisit in v3.4 with a
-- proper memory_recall_log table if the operator-value of unique
-- queries justifies the cost.
--
-- Idempotent: every ALTER / CREATE uses IF NOT EXISTS.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS recall_count integer NOT NULL DEFAULT 0;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS last_recalled_at timestamptz;

-- Partial index on last_recalled_at — most rows have NULL on that
-- column (memories that have never been searched). The partial form
-- keeps the index tiny and the "recent recalls" query fast.
CREATE INDEX IF NOT EXISTS idx_memories_last_recalled_at
    ON memories(last_recalled_at DESC)
    WHERE last_recalled_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_recall_count
    ON memories(recall_count DESC)
    WHERE recall_count > 0;
