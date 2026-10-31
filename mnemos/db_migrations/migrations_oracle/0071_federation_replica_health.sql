-- migration: 0071_federation_replica_health
-- target:    Oracle 19c+
-- purpose:   M12 items 1 + 2 — per-peer lag/health sample and the peer's
--            embedder fingerprint for /v1/federation/status.
--
-- Oracle schema provisioning replays every migration at startup; ADD replays
-- are benign because schema.py treats ORA-01430 (column exists) as a no-op
-- for ALTER statements, the same contract 0066 relies on.

ALTER TABLE federation_peers ADD (last_cursor_ts TIMESTAMP WITH TIME ZONE);
ALTER TABLE federation_peers ADD (lag_seconds BINARY_DOUBLE);
ALTER TABLE federation_peers ADD (rows_last_sync NUMBER);
ALTER TABLE federation_peers ADD (consecutive_failures NUMBER DEFAULT 0 NOT NULL);
ALTER TABLE federation_peers ADD (peer_embed_model VARCHAR2(200));
ALTER TABLE federation_peers ADD (peer_embed_dim NUMBER);
ALTER TABLE federation_peers ADD (embedding_status VARCHAR2(1000));
