-- migration: 0030_memory_audit_roots
-- target:    PostgreSQL 16
-- purpose:   v6.2 M-2.2.1 — sealed-window Merkle root ledger.
-- design:    docs/v6.2-nexus-pattern-adoption.md § 1
-- Mirrors db/migrations_oracle/0030_memory_audit_roots.sql.

CREATE TABLE IF NOT EXISTS memory_audit_roots (
  global_root     BYTEA       NOT NULL,
  window_start    TIMESTAMPTZ NOT NULL,
  window_end      TIMESTAMPTZ NOT NULL,
  entry_count     BIGINT      NOT NULL,
  root_signature  BYTEA       NOT NULL,
  signer_pubkey   BYTEA       NOT NULL,
  sealed_at       TIMESTAMPTZ NOT NULL,
  CONSTRAINT pk_memory_audit_roots PRIMARY KEY (global_root),
  CONSTRAINT ck_memory_audit_roots_window
    CHECK (window_end > window_start),
  CONSTRAINT ck_memory_audit_roots_lengths
    CHECK (
      octet_length(global_root) = 32 AND
      octet_length(root_signature) = 64 AND
      octet_length(signer_pubkey) = 32
    )
);

CREATE INDEX IF NOT EXISTS ix_memory_audit_roots_window
  ON memory_audit_roots(window_end DESC);
