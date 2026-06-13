-- 0002c_graeae_parity_cols.sql — owner/namespace scoping + soft-delete on the
-- GRAEAE tables (cross-backend parity). The canonical schema (sqlite
-- migrations.sql + the mysql parity port 0002_feature_parity_schema.sql) carries
-- owner_id / namespace / deleted_at on graeae_consultations and deleted_at on
-- graeae_audit_log. The Oracle 0002_graeae.sql (which 0002b mirrored) predates
-- ownership + soft-delete and OMITS them — so OracleConsultationsRepository's
-- read methods (which filter c.owner_id / c.namespace / deleted_at) never had a
-- schema to run against. Db2 native Consultations needs these columns; add them
-- here so Db2ConsultationsRepository can reach parity with postgres/sqlite.
--
-- One ALTER per ADD COLUMN (Db2 accepts a comma-less multi-clause ALTER, but
-- separate statements are unambiguous + idempotent: a re-run hits SQL0612N /
-- SQLSTATE 42711 "column already exists", which the applier treats as benign).

ALTER TABLE graeae_consultations ADD COLUMN owner_id   VARCHAR(255) NOT NULL DEFAULT 'default';
ALTER TABLE graeae_consultations ADD COLUMN namespace  VARCHAR(255) NOT NULL DEFAULT 'default';
ALTER TABLE graeae_consultations ADD COLUMN deleted_at TIMESTAMP;

CALL SYSPROC.ADMIN_CMD('REORG TABLE graeae_consultations');

ALTER TABLE graeae_audit_log ADD COLUMN deleted_at TIMESTAMP;

CALL SYSPROC.ADMIN_CMD('REORG TABLE graeae_audit_log');

CREATE INDEX idx_graeae_consult_owner_ns ON graeae_consultations (owner_id, namespace);
