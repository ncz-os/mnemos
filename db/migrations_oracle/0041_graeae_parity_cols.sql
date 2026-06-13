-- 0041_graeae_parity_cols.sql — ownership + soft-delete on the GRAEAE tables
-- (cross-backend parity). graeae_consultations predates ownership + soft-delete
-- and omits owner_id/namespace/deleted_at; OracleConsultationsRepository's
-- create_consultation_with_audit already binds owner_id/namespace and the read
-- methods filter them, so without these columns Oracle consultations never had
-- a schema to run against. Mirrors db/migrations_db2/0002c_graeae_parity_cols.sql
-- and the canonical sqlite/mysql schema.

ALTER TABLE graeae_consultations ADD (
  owner_id   VARCHAR2(255) DEFAULT 'default' NOT NULL,
  namespace  VARCHAR2(255) DEFAULT 'default' NOT NULL,
  deleted_at TIMESTAMP WITH TIME ZONE
);

ALTER TABLE graeae_audit_log ADD (deleted_at TIMESTAMP WITH TIME ZONE);

CREATE INDEX idx_graeae_consult_owner_ns ON graeae_consultations (owner_id, namespace);
