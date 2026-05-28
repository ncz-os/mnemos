-- migration: 0029_memory_audit_chain
-- target:    IBM Db2 12.1.x
-- purpose:   v6.2 M-2.2.1 — per-memory append-only audit chain.
-- design:    docs/v6.2-nexus-pattern-adoption.md § 1
-- Mirrors db/migrations_oracle/0029_memory_audit_chain.sql.

CREATE TABLE memory_audit_chain (
  entry_id         VARBINARY(16) NOT NULL,
  memory_id        VARBINARY(16) NOT NULL,
  prev_entry_id    VARBINARY(16),
  prev_entry_hash  VARBINARY(32),
  op               VARCHAR(16)   NOT NULL,
  payload_hash     VARBINARY(32) NOT NULL,
  writer_id        VARCHAR(128)  NOT NULL,
  writer_pubkey    VARBINARY(32) NOT NULL,
  signature        VARBINARY(64) NOT NULL,
  signed_at        TIMESTAMP     NOT NULL,
  global_root      VARBINARY(32),
  global_seq       BIGINT,
  CONSTRAINT pk_memory_audit_chain PRIMARY KEY (entry_id),
  CONSTRAINT ck_memory_audit_op
    CHECK (op IN ('create','update','delete','archive','replicate'))
);

CREATE INDEX ix_memory_audit_by_memory
  ON memory_audit_chain(memory_id, signed_at DESC);
CREATE INDEX ix_memory_audit_by_root
  ON memory_audit_chain(global_root);
-- Db2 partial-index equivalent: separate index sorted by signed_at;
-- sealer query adds WHERE global_root IS NULL predicate.
CREATE INDEX ix_memory_audit_unsigned
  ON memory_audit_chain(signed_at);
CREATE INDEX ix_memory_audit_by_writer
  ON memory_audit_chain(writer_id, signed_at DESC);
