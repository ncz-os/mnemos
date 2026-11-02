-- FB103: exact-match metadata read-back (GET /v1/memories?meta=job_id:X).
--
-- SQLite uses an expression index only when the query repeats the indexed
-- expression exactly, and an expression index is bound to ONE path: there is
-- no jsonb-style containment index on SQLite to make this generic. So this is
-- the index for the read-back key the doctor actually filters on (job_id,
-- see audit-mnemos-agent-status-20261102); other metadata keys are answered
-- correctly but scan.
CREATE INDEX IF NOT EXISTS idx_memories_metadata_job_id
    ON memories (json_extract(metadata, '$."job_id"'));
