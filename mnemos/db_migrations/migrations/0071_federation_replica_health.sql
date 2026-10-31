-- migration: 0071_federation_replica_health
-- target:    PostgreSQL 16
-- purpose:   M12 items 1 + 2 — per-peer lag/health sample and the peer's
--            embedder fingerprint, so /v1/federation/status can report why a
--            replica is behind and whether vectors were copied.
-- replay:    every statement is IF NOT EXISTS; this series is re-run on start.

ALTER TABLE federation_peers
  ADD COLUMN IF NOT EXISTS last_cursor_ts TIMESTAMPTZ;

ALTER TABLE federation_peers
  ADD COLUMN IF NOT EXISTS lag_seconds DOUBLE PRECISION;

ALTER TABLE federation_peers
  ADD COLUMN IF NOT EXISTS rows_last_sync INTEGER;

ALTER TABLE federation_peers
  ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0;

ALTER TABLE federation_peers
  ADD COLUMN IF NOT EXISTS peer_embed_model TEXT;

ALTER TABLE federation_peers
  ADD COLUMN IF NOT EXISTS peer_embed_dim INTEGER;

ALTER TABLE federation_peers
  ADD COLUMN IF NOT EXISTS embedding_status TEXT;
