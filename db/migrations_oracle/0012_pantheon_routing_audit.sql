-- 0012_pantheon_routing_audit.sql — Oracle 23ai parity backfill.
-- Dedicated PANTHEON routing audit table matching the v4.2 SQLite/Postgres
-- shape. Keeps routing telemetry out of memories on Oracle deployments.
-- Idempotent: table/index creation is guarded for replay on hot-patched live DBs.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables
   WHERE table_name = 'PANTHEON_ROUTING_AUDIT';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE pantheon_routing_audit (
        id             VARCHAR2(36) DEFAULT LOWER(RAWTOHEX(SYS_GUID())) PRIMARY KEY,
        request_id     VARCHAR2(256),
        tenant_user_id VARCHAR2(256),
        alias_or_model VARCHAR2(512),
        resolved_to    VARCHAR2(512),
        outcome        VARCHAR2(64),
        latency_ms     NUMBER(12),
        tokens_in      NUMBER(12),
        tokens_out     NUMBER(12),
        cost_usd       NUMBER(10,4),
        error_class    VARCHAR2(256),
        payload        CLOB NOT NULL CHECK (payload IS JSON),
        created        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
      )
    ]';
  END IF;
END;
/

DECLARE
  e_exists EXCEPTION;
  PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
  EXECUTE IMMEDIATE
    'CREATE INDEX idx_pantheon_routing_audit_created_desc ON pantheon_routing_audit (created DESC)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

DECLARE
  e_exists EXCEPTION;
  PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
  EXECUTE IMMEDIATE
    'CREATE INDEX idx_pantheon_routing_audit_tenant_created_desc ON pantheon_routing_audit (tenant_user_id, created DESC)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
