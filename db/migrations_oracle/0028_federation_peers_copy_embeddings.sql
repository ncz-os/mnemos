-- migration: 0028_federation_peers_copy_embeddings
-- target:    Oracle 23ai PDB ORCLPDB1
-- purpose:   v6.1 F-1 — add opt-in per-peer flag controlling whether
--            /v1/federation/feed payload includes the embedding column.
-- design:    docs/v6.1-federation-embeddings-copy.md
--
-- Idempotency: guarded by USER_TAB_COLUMNS check.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
   WHERE table_name = 'FEDERATION_PEERS'
     AND column_name = 'COPY_EMBEDDINGS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      ALTER TABLE federation_peers
        ADD copy_embeddings NUMBER(1) DEFAULT 0 NOT NULL
    ]';
    EXECUTE IMMEDIATE q'[
      ALTER TABLE federation_peers
        ADD CONSTRAINT ck_federation_peer_copy_embeddings
        CHECK (copy_embeddings IN (0,1))
    ]';
  END IF;
END;
/

COMMIT;
