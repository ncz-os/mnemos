-- migration: 0071_federation_replica_health
-- target:    MariaDB 11.7+ / MySQL 8.0+ (one file, wire-compatible dialects)
-- purpose:   M12 items 1 + 2 — per-peer lag/health sample and the peer's
--            embedder fingerprint for /v1/federation/status.
--
-- MariaDB 11.7 supports ADD COLUMN IF NOT EXISTS; the runner also treats
-- error 1060 (duplicate column) as already-applied.

ALTER TABLE federation_peers ADD COLUMN IF NOT EXISTS last_cursor_ts TIMESTAMP(6) NULL;
ALTER TABLE federation_peers ADD COLUMN IF NOT EXISTS lag_seconds DOUBLE NULL;
ALTER TABLE federation_peers ADD COLUMN IF NOT EXISTS rows_last_sync INT NULL;
ALTER TABLE federation_peers ADD COLUMN IF NOT EXISTS consecutive_failures INT NOT NULL DEFAULT 0;
ALTER TABLE federation_peers ADD COLUMN IF NOT EXISTS peer_embed_model VARCHAR(256) NULL;
ALTER TABLE federation_peers ADD COLUMN IF NOT EXISTS peer_embed_dim INT NULL;
ALTER TABLE federation_peers ADD COLUMN IF NOT EXISTS embedding_status TEXT NULL;
