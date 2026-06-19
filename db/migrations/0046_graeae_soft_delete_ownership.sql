-- 0046_graeae_soft_delete_ownership.sql — PostgreSQL (canonical).
--
-- Numbered-contract anchor for the GRAEAE ownership + soft-delete
-- columns that the implemented consultation read/write paths depend on:
--   * OracleConsultationsRepository / PostgresConsultationsRepository
--     scope consultation reads on owner_id + namespace and filter
--     deleted_at IS NULL.
--   * create_consultation_with_audit INSERTs owner_id + namespace.
--
-- On PostgreSQL these columns already arrive via the legacy flat-file
-- migrations (migrations_v3_ownership.sql,
-- migrations_v3_5_sessions_consultations_namespace.sql,
-- migrations_v4_2_deletion_requests_soft_delete_columns.sql). This file
-- restates them idempotently so the NNNN parity contract carries a
-- canonical entry with Oracle / Db2 siblings of the same basename
-- (db/migrations_oracle, db/migrations_db2). Safe to re-run.

ALTER TABLE graeae_consultations
    ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT 'default';

ALTER TABLE graeae_consultations
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';

ALTER TABLE graeae_consultations
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

ALTER TABLE graeae_audit_log
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_graeae_consultations_owner_namespace
    ON graeae_consultations (owner_id, namespace);
