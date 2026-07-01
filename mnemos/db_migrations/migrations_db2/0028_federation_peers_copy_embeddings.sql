-- migration: 0028_federation_peers_copy_embeddings
-- target:    IBM Db2 12.1.x
-- purpose:   v6.1 F-1 — add opt-in per-peer flag controlling whether
--            /v1/federation/feed payload includes the embedding column.
-- design:    docs/v6.1-federation-embeddings-copy.md

ALTER TABLE federation_peers
  ADD COLUMN copy_embeddings SMALLINT NOT NULL WITH DEFAULT 0;

ALTER TABLE federation_peers
  ADD CONSTRAINT ck_federation_peer_copy_embeddings
  CHECK (copy_embeddings IN (0,1));

-- Db2 requires REORG after adding NOT NULL WITH DEFAULT in some configs.
-- Migration runner handles the REORG dispatch; this file stays declarative.
