-- migration: 0030_memory_audit_roots
-- target:    Oracle 23ai PDB ORCLPDB1
-- purpose:   v6.2 M-2.2.1 — sealed-window Merkle root ledger. Each row =
--            one 60-second window. Sealer worker writes one row per seal.
--            Federation peers fetch via /v1/federation/audit_roots.
-- design:    docs/v6.2-nexus-pattern-adoption.md § 1
--
-- Notes:
--   - global_root is the Merkle root of entries with signed_at in
--     [window_start, window_end).
--   - entry_count is the leaf count (pre-pad to power of 2).
--   - root_signature is Ed25519(global_root || window_start || window_end)
--     signed by the per-instance root key.
--   - Append-only; sealer never updates a sealed row.
--
-- Idempotency: guarded by USER_TABLES.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'MEMORY_AUDIT_ROOTS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE memory_audit_roots (
        global_root       RAW(32)        NOT NULL,
        window_start      TIMESTAMP WITH TIME ZONE NOT NULL,
        window_end        TIMESTAMP WITH TIME ZONE NOT NULL,
        entry_count       NUMBER(12)     NOT NULL,
        root_signature    RAW(64)        NOT NULL,
        signer_pubkey     RAW(32)        NOT NULL,
        sealed_at         TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT pk_memory_audit_roots PRIMARY KEY (global_root),
        CONSTRAINT ck_memory_audit_roots_window
          CHECK (window_end > window_start)
      )
    ]';
  END IF;
END;
/

DECLARE
  PROCEDURE create_index(p_name VARCHAR2, p_ddl VARCHAR2) IS
    v_n NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_n FROM user_indexes WHERE index_name = p_name;
    IF v_n = 0 THEN EXECUTE IMMEDIATE p_ddl; END IF;
  END;
BEGIN
  -- Replica federation walk: pull windows since last seen.
  create_index('IX_MEMORY_AUDIT_ROOTS_WINDOW',
               'CREATE INDEX ix_memory_audit_roots_window ON memory_audit_roots(window_end DESC)');
END;
/

COMMIT;
