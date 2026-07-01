-- migration: v6_2_audit_chain_sqlite
-- target:    SQLite 3.40+ (BLOB columns; no CHECK on length below)
-- purpose:   v6.2 M-2.2.1 — per-memory append-only audit chain (SQLite shape).
-- design:    docs/v6.2-nexus-pattern-adoption.md § 1.
-- Mirrors db/migrations/0029_memory_audit_chain.sql +
--         db/migrations/0030_memory_audit_roots.sql.
--
-- Length CHECK constraints from Postgres/Oracle are dropped here —
-- SQLite typing is dynamic and the audit application layer enforces
-- byte lengths (see mnemos/audit/writer.py::build_entry). Single-
-- writer assumption is fine for the edge replica role (per v6.1 P3
-- #43 "SQLite is dev-only").

CREATE TABLE IF NOT EXISTS memory_audit_chain (
  entry_id         BLOB NOT NULL,
  memory_id        BLOB NOT NULL,
  prev_entry_id    BLOB,
  prev_entry_hash  BLOB,
  op               TEXT NOT NULL CHECK (op IN ('create','update','delete','archive','replicate')),
  payload_hash     BLOB NOT NULL,
  writer_id        TEXT NOT NULL,
  writer_pubkey    BLOB NOT NULL,
  signature        BLOB NOT NULL,
  signed_at        TEXT NOT NULL,
  global_root      BLOB,
  global_seq       INTEGER,
  PRIMARY KEY (entry_id)
);

CREATE INDEX IF NOT EXISTS ix_memory_audit_by_memory
  ON memory_audit_chain(memory_id, signed_at DESC);

CREATE INDEX IF NOT EXISTS ix_memory_audit_by_root
  ON memory_audit_chain(global_root);

-- SQLite partial index — same shape as PG/Oracle ix_memory_audit_unsigned.
CREATE INDEX IF NOT EXISTS ix_memory_audit_unsigned
  ON memory_audit_chain(signed_at) WHERE global_root IS NULL;

CREATE INDEX IF NOT EXISTS ix_memory_audit_by_writer
  ON memory_audit_chain(writer_id, signed_at DESC);


CREATE TABLE IF NOT EXISTS memory_audit_roots (
  global_root     BLOB NOT NULL,
  window_start    TEXT NOT NULL,
  window_end      TEXT NOT NULL,
  entry_count     INTEGER NOT NULL,
  root_signature  BLOB NOT NULL,
  signer_pubkey   BLOB NOT NULL,
  sealed_at       TEXT NOT NULL,
  PRIMARY KEY (global_root),
  CHECK (window_end > window_start)
);

CREATE INDEX IF NOT EXISTS ix_memory_audit_roots_window
  ON memory_audit_roots(window_end DESC);
