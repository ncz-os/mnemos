--#SET TERMINATOR @
-- 0046_graeae_soft_delete_ownership.sql — Db2 12.1.5 (Oracle Compat) parity.
-- Mirrors db/migrations/0046_graeae_soft_delete_ownership.sql and
-- db/migrations_oracle/0046_graeae_soft_delete_ownership.sql.
--
-- Backfills the ownership + soft-delete columns the implemented GRAEAE
-- consultation read/write path requires but which 0002_graeae.sql never
-- defined for Db2: consultation reads scope on owner_id / namespace and
-- filter deleted_at IS NULL; create_consultation_with_audit INSERTs
-- owner_id + namespace. Idempotent via syscat.columns existence guards
-- and a duplicate-object handler on the index.

BEGIN
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'GRAEAE_CONSULTATIONS' AND colname = 'OWNER_ID') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE graeae_consultations ADD COLUMN owner_id VARCHAR(100) NOT NULL WITH DEFAULT ''default''';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'GRAEAE_CONSULTATIONS' AND colname = 'NAMESPACE') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE graeae_consultations ADD COLUMN namespace VARCHAR(100) NOT NULL WITH DEFAULT ''default''';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'GRAEAE_CONSULTATIONS' AND colname = 'DELETED_AT') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE graeae_consultations ADD COLUMN deleted_at TIMESTAMP(6) WITH TIME ZONE DEFAULT NULL';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'GRAEAE_AUDIT_LOG' AND colname = 'DELETED_AT') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE graeae_audit_log ADD COLUMN deleted_at TIMESTAMP(6) WITH TIME ZONE DEFAULT NULL';
  END IF;
END@

BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END; EXECUTE IMMEDIATE 'CREATE INDEX idx_graeae_cons_owner_ns ON graeae_consultations (owner_id, namespace)'; END@
