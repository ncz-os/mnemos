-- migration: 0028_federation_peers_copy_embeddings
-- target:    PostgreSQL 16
-- schema:    public
-- purpose:   v6.1 F-1 — add opt-in per-peer flag controlling whether
--            /v1/federation/feed payload includes the embedding column.
--            Default off preserves v6.0 bandwidth profile + behavior.
-- design:    docs/v6.1-federation-embeddings-copy.md

ALTER TABLE federation_peers
  ADD COLUMN IF NOT EXISTS copy_embeddings SMALLINT NOT NULL DEFAULT 0;

-- PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS. Migrations here carry no
-- applied-state table and are re-run on every start, so a bare ADD CONSTRAINT
-- raises "already exists" on the second run -- and because that aborts the
-- surrounding transaction, every statement after it in this file is skipped
-- too. Guarding on pg_constraint keeps the file replayable.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_federation_peer_copy_embeddings'
      AND conrelid = 'federation_peers'::regclass
  ) THEN
    ALTER TABLE federation_peers
      ADD CONSTRAINT ck_federation_peer_copy_embeddings
        CHECK (copy_embeddings IN (0,1));
  END IF;
END $$;
