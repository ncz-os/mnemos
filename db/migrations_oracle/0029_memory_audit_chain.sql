-- migration: 0029_memory_audit_chain
-- target:    Oracle 23ai PDB ORCLPDB1
-- purpose:   v6.2 M-2.2.1 — per-memory append-only audit chain. One entry
--            per memory write (create/update/delete/archive). Linear chain
--            via prev_entry_hash; global Merkle root populated by sealer
--            worker on 1-minute cadence.
-- design:    docs/v6.2-nexus-pattern-adoption.md § 1
--
-- Notes:
--   - entry_id is UUIDv7 stored as RAW(16) for sortability + compactness.
--   - payload_hash + prev_entry_hash are SHA-256 → RAW(32).
--   - signature is Ed25519 → RAW(64).
--   - writer_pubkey is Ed25519 public key → RAW(32).
--   - global_root + global_seq populated post-seal; queryable via
--     ix_memory_audit_unsigned to find work to seal.
--
-- Append-only invariant enforced application-side; no UPDATE on the data
-- columns. Sealer worker UPDATEs only global_root + global_seq.
--
-- Idempotency: guarded by USER_TABLES.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'MEMORY_AUDIT_CHAIN';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE memory_audit_chain (
        entry_id         RAW(16)        NOT NULL,
        memory_id        RAW(16)        NOT NULL,
        prev_entry_id    RAW(16),
        prev_entry_hash  RAW(32),
        op               VARCHAR2(16)   NOT NULL,
        payload_hash     RAW(32)        NOT NULL,
        writer_id        VARCHAR2(128)  NOT NULL,
        writer_pubkey    RAW(32)        NOT NULL,
        signature        RAW(64)        NOT NULL,
        signed_at        TIMESTAMP WITH TIME ZONE NOT NULL,
        global_root      RAW(32),
        global_seq       NUMBER(15),
        CONSTRAINT pk_memory_audit_chain PRIMARY KEY (entry_id),
        CONSTRAINT ck_memory_audit_op
          CHECK (op IN ('create','update','delete','archive','replicate'))
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
  -- Per-memory chain walk: (memory_id, signed_at) supports verify-from-newest.
  create_index('IX_MEMORY_AUDIT_BY_MEMORY',
               'CREATE INDEX ix_memory_audit_by_memory ON memory_audit_chain(memory_id, signed_at DESC)');
  -- Window lookup by Merkle root for proof requests.
  create_index('IX_MEMORY_AUDIT_BY_ROOT',
               'CREATE INDEX ix_memory_audit_by_root ON memory_audit_chain(global_root)');
  -- Sealer worker queue: find entries not yet sealed, ordered by signed_at.
  -- Oracle doesn't support PARTIAL INDEX (WHERE clause on CREATE INDEX); use
  -- a function-based index that emits NULL for sealed rows so they don't take
  -- index space. Sealer queries use the same CASE predicate so the optimizer
  -- picks up the FBI. Same effective storage shape as PG/Db2 partial index.
  create_index('IX_MEMORY_AUDIT_UNSIGNED',
               'CREATE INDEX ix_memory_audit_unsigned ON memory_audit_chain('
               || 'CASE WHEN global_root IS NULL THEN signed_at END)');
  -- Writer accountability + per-writer chain audit.
  create_index('IX_MEMORY_AUDIT_BY_WRITER',
               'CREATE INDEX ix_memory_audit_by_writer ON memory_audit_chain(writer_id, signed_at DESC)');
END;
/

COMMIT;
