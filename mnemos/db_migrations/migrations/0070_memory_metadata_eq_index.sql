-- FB103: exact-match metadata read-back (GET /v1/memories?meta=job_id:X).
--
-- The filter emits ``metadata @> $n::jsonb`` with the key and the value both
-- bound. jsonb_path_ops is the operator class that indexes that containment
-- operator, and it is generic in the key, so one index covers every metadata
-- field a caller filters on (no per-key index, no write-path change).
--
-- CONCURRENTLY is deliberately absent: this directory's migrations run inside
-- the schema-bootstrap transaction (see mnemos/persistence/schema.py), and a
-- non-concurrent GIN build is what every other index in this tree does.
CREATE INDEX IF NOT EXISTS idx_memories_metadata_path_ops
    ON memories USING gin (metadata jsonb_path_ops);
