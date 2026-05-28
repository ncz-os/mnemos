-- migration: 0030_memory_audit_roots
-- target:    IBM Db2 12.1.x
-- purpose:   v6.2 M-2.2.1 — sealed-window Merkle root ledger.
-- design:    docs/v6.2-nexus-pattern-adoption.md § 1
-- Mirrors db/migrations_oracle/0030_memory_audit_roots.sql.

CREATE TABLE memory_audit_roots (
  global_root     VARBINARY(32) NOT NULL,
  window_start    TIMESTAMP     NOT NULL,
  window_end      TIMESTAMP     NOT NULL,
  entry_count     BIGINT        NOT NULL,
  root_signature  VARBINARY(64) NOT NULL,
  signer_pubkey   VARBINARY(32) NOT NULL,
  sealed_at       TIMESTAMP     NOT NULL,
  CONSTRAINT pk_memory_audit_roots PRIMARY KEY (global_root),
  CONSTRAINT ck_memory_audit_roots_window
    CHECK (window_end > window_start)
);

CREATE INDEX ix_memory_audit_roots_window
  ON memory_audit_roots(window_end DESC);
