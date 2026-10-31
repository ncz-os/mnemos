-- migration: 0071_federation_replica_health
-- target:    IBM Db2 12.1.x
-- purpose:   M12 items 1 + 2 — per-peer lag/health sample and the peer's
--            embedder fingerprint for /v1/federation/status.
--
-- Db2 has no ADD COLUMN IF NOT EXISTS; the runner treats SQL0605W
-- (column already exists) as already-applied, matching 0028's contract.

ALTER TABLE federation_peers ADD COLUMN last_cursor_ts TIMESTAMP;
ALTER TABLE federation_peers ADD COLUMN lag_seconds DOUBLE;
ALTER TABLE federation_peers ADD COLUMN rows_last_sync BIGINT;
ALTER TABLE federation_peers ADD COLUMN consecutive_failures INTEGER NOT NULL WITH DEFAULT 0;
ALTER TABLE federation_peers ADD COLUMN peer_embed_model VARCHAR(200);
ALTER TABLE federation_peers ADD COLUMN peer_embed_dim INTEGER;
ALTER TABLE federation_peers ADD COLUMN embedding_status CLOB;
