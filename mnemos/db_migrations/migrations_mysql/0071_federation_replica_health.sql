-- migration: 0071_federation_replica_health
-- target:    MySQL 8.0+
-- purpose:   M12 items 1 + 2 — per-peer lag/health sample and the peer's
--            embedder fingerprint for /v1/federation/status.
--
-- MySQL has no ADD COLUMN IF NOT EXISTS (that is MariaDB syntax), so a
-- replay on an already-migrated database raises error 1060. The provisioning
-- helper below swallows exactly that code (see
-- _ensure_mysql_federation_replica_health in mnemos/persistence/mysql.py),
-- the same contract migrations_mysql/0065 uses.

ALTER TABLE federation_peers ADD COLUMN last_cursor_ts TIMESTAMP(6) NULL;
ALTER TABLE federation_peers ADD COLUMN lag_seconds DOUBLE NULL;
ALTER TABLE federation_peers ADD COLUMN rows_last_sync INT NULL;
ALTER TABLE federation_peers ADD COLUMN consecutive_failures INT NOT NULL DEFAULT 0;
ALTER TABLE federation_peers ADD COLUMN peer_embed_model VARCHAR(256) NULL;
ALTER TABLE federation_peers ADD COLUMN peer_embed_dim INT NULL;
ALTER TABLE federation_peers ADD COLUMN embedding_status TEXT NULL;
