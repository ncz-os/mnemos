-- 0012_pantheon_routing_audit.sql — Db2 parity backfill.
-- Dedicated PANTHEON routing audit table matching the v4.2 SQLite/Postgres
-- shape. Keeps routing telemetry out of memories on Db2 deployments.
-- Statement terminator: @

BEGIN NOT ATOMIC
  IF NOT EXISTS (
    SELECT 1 FROM SYSCAT.TABLES
    WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'PANTHEON_ROUTING_AUDIT'
  ) THEN
    EXECUTE IMMEDIATE '
      CREATE TABLE pantheon_routing_audit (
        id             VARCHAR(36) NOT NULL PRIMARY KEY,
        request_id     VARCHAR(256),
        tenant_user_id VARCHAR(256),
        alias_or_model VARCHAR(512),
        resolved_to    VARCHAR(512),
        outcome        VARCHAR(64),
        latency_ms     INTEGER,
        tokens_in      INTEGER,
        tokens_out     INTEGER,
        cost_usd       DECIMAL(10,4),
        error_class    VARCHAR(256),
        payload        CLOB(1M) NOT NULL,
        created        TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL
      )';
  END IF;
END@

BEGIN NOT ATOMIC
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_pantheon_routing_audit_created_desc ON pantheon_routing_audit (created DESC)';
END@

BEGIN NOT ATOMIC
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_pantheon_routing_audit_tenant_created_desc ON pantheon_routing_audit (tenant_user_id, created DESC)';
END@
