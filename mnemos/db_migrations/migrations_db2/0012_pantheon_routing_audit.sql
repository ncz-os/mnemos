--#SET TERMINATOR @
-- 0012_pantheon_routing_audit.sql — Db2 12.1.5 parity backfill.
-- Mirrors db/migrations_v4_2_pantheon_routing_audit.sql. Idempotent via
-- duplicate-object handlers and column-existence guards.

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE pantheon_routing_audit (
      id             VARCHAR(36) NOT NULL,
      request_id     VARCHAR(256),
      tenant_user_id VARCHAR(256),
      alias_or_model VARCHAR(256),
      resolved_to    VARCHAR(256),
      outcome        VARCHAR(64),
      latency_ms     INTEGER,
      tokens_in      INTEGER,
      tokens_out     INTEGER,
      cost_usd       DECIMAL(10,4),
      error_class    VARCHAR(256),
      payload        CLOB(2M) INLINE LENGTH 4096 NOT NULL
        CHECK (SYSTOOLS.JSON2BSON(payload) IS NOT NULL),
      created        TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP,
      CONSTRAINT pk_pantheon_routing_audit PRIMARY KEY (id)
    )';
END@

-- Backfill columns if an earlier stub table exists. Existing rows may lack a
-- full routing payload, so payload is nullable on the ALTER path; fresh creates
-- above keep the NOT NULL + JSON contract.
BEGIN
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'REQUEST_ID') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN request_id VARCHAR(256)';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'TENANT_USER_ID') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN tenant_user_id VARCHAR(256)';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'ALIAS_OR_MODEL') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN alias_or_model VARCHAR(256)';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'RESOLVED_TO') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN resolved_to VARCHAR(256)';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'OUTCOME') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN outcome VARCHAR(64)';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'LATENCY_MS') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN latency_ms INTEGER';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'TOKENS_IN') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN tokens_in INTEGER';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'TOKENS_OUT') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN tokens_out INTEGER';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'COST_USD') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN cost_usd DECIMAL(10,4)';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'ERROR_CLASS') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN error_class VARCHAR(256)';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'PAYLOAD') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN payload CLOB(2M) INLINE LENGTH 4096';
  END IF;
  IF (SELECT COUNT(*) FROM syscat.columns WHERE tabschema = CURRENT SCHEMA AND tabname = 'PANTHEON_ROUTING_AUDIT' AND colname = 'CREATED') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD COLUMN created TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP';
  END IF;
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TRIGGER pra_bi_id NO CASCADE BEFORE INSERT ON pantheon_routing_audit
    REFERENCING NEW AS n FOR EACH ROW
    WHEN (n.id IS NULL)
      SET n.id = LOWER(HEX(GENERATE_UNIQUE()))';
END@

BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_pantheon_routing_audit_created_desc ON pantheon_routing_audit (created DESC)';
END@

BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_pantheon_routing_audit_tenant_created_desc ON pantheon_routing_audit (tenant_user_id, created DESC)';
END@
