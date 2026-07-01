-- migration: 0029_memory_audit_chain
-- target:    PostgreSQL 16
-- purpose:   v6.2 M-2.2.1 — per-memory append-only audit chain.
-- design:    docs/v6.2-nexus-pattern-adoption.md § 1
-- Mirrors db/migrations_oracle/0029_memory_audit_chain.sql.

CREATE TABLE IF NOT EXISTS memory_audit_chain (
  entry_id         BYTEA       NOT NULL,
  memory_id        BYTEA       NOT NULL,
  prev_entry_id    BYTEA,
  prev_entry_hash  BYTEA,
  op               VARCHAR(16) NOT NULL,
  payload_hash     BYTEA       NOT NULL,
  writer_id        VARCHAR(128) NOT NULL,
  writer_pubkey    BYTEA       NOT NULL,
  signature        BYTEA       NOT NULL,
  signed_at        TIMESTAMPTZ NOT NULL,
  global_root      BYTEA,
  global_seq       BIGINT,
  CONSTRAINT pk_memory_audit_chain PRIMARY KEY (entry_id),
  CONSTRAINT ck_memory_audit_op
    CHECK (op IN ('create','update','delete','archive','replicate')),
  CONSTRAINT ck_memory_audit_lengths
    CHECK (
      octet_length(entry_id) = 16 AND
      octet_length(memory_id) = 16 AND
      (prev_entry_id IS NULL OR octet_length(prev_entry_id) = 16) AND
      (prev_entry_hash IS NULL OR octet_length(prev_entry_hash) = 32) AND
      octet_length(payload_hash) = 32 AND
      octet_length(writer_pubkey) = 32 AND
      octet_length(signature) = 64 AND
      (global_root IS NULL OR octet_length(global_root) = 32)
    )
);

CREATE INDEX IF NOT EXISTS ix_memory_audit_by_memory
  ON memory_audit_chain(memory_id, signed_at DESC);
CREATE INDEX IF NOT EXISTS ix_memory_audit_by_root
  ON memory_audit_chain(global_root);
CREATE INDEX IF NOT EXISTS ix_memory_audit_unsigned
  ON memory_audit_chain(signed_at) WHERE global_root IS NULL;
CREATE INDEX IF NOT EXISTS ix_memory_audit_by_writer
  ON memory_audit_chain(writer_id, signed_at DESC);
