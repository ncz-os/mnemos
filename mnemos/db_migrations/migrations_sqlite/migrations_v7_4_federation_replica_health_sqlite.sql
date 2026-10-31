-- M12 replica observability — SQLite mirror of 0071_federation_replica_health.
--
-- federation_peers gains the per-peer lag/health sample (decision M12 item 1)
-- and the peer's embedder fingerprint (M12 item 2):
--
--   last_cursor_ts        -- newest remote event time this replica applied
--   lag_seconds           -- age of that cursor, sampled at sync end
--   rows_last_sync        -- rows moved by the last successful pass
--   consecutive_failures  -- resets to 0 only on a successful pass
--   peer_embed_model / peer_embed_dim -- peer's advertised embedder identity
--   embedding_status      -- why vectors were/weren't copied, operator text
--
-- SQLite has no ADD COLUMN IF NOT EXISTS; the sqlite runner treats a
-- "duplicate column name" error as already-applied (see _run_migrations), so
-- a fresh database and an upgraded one end in the same shape either way.
-- A fresh database already gets these columns from migrations.sql; on an
-- existing file every statement below is a tolerated duplicate-column error.
-- ---------------------------------------------------------------------------

ALTER TABLE federation_peers ADD COLUMN last_cursor_ts TEXT;
ALTER TABLE federation_peers ADD COLUMN lag_seconds REAL;
ALTER TABLE federation_peers ADD COLUMN rows_last_sync INTEGER;
ALTER TABLE federation_peers ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE federation_peers ADD COLUMN peer_embed_model TEXT;
ALTER TABLE federation_peers ADD COLUMN peer_embed_dim INTEGER;
ALTER TABLE federation_peers ADD COLUMN embedding_status TEXT;
