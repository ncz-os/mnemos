-- 0012_pantheon_routing_audit.sql — Oracle 23ai parity backfill.
-- Mirrors db/migrations_v4_2_pantheon_routing_audit.sql. Historical drift
-- shipped this audit only for SQLite/PostgreSQL, which let Oracle-backed
-- PANTHEON fall back to memories-table telemetry. Idempotent guards make the
-- migration safe on databases already hot-patched live or partially migrated.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'PANTHEON_ROUTING_AUDIT';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE pantheon_routing_audit (
        id             VARCHAR2(36) NOT NULL,
        request_id     VARCHAR2(256),
        tenant_user_id VARCHAR2(256),
        alias_or_model VARCHAR2(256),
        resolved_to    VARCHAR2(256),
        outcome        VARCHAR2(64),
        latency_ms     NUMBER(12),
        tokens_in      NUMBER(12),
        tokens_out     NUMBER(12),
        cost_usd       NUMBER(10,4),
        error_class    VARCHAR2(256),
        payload        CLOB NOT NULL CHECK (payload IS JSON),
        created        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP,
        CONSTRAINT pk_pantheon_routing_audit PRIMARY KEY (id)
      )
    ]';
  END IF;
END;
/

-- Backfill columns if an earlier stub table exists (consultation_id/muse/
-- prompt_hash/chosen_model/routing_reason). Existing rows may not have a full
-- payload, so payload is nullable on the ALTER path; fresh creates above keep
-- the NOT NULL + IS JSON contract.
DECLARE
  v_count NUMBER;
  PROCEDURE add_col(p_col VARCHAR2, p_ddl VARCHAR2) IS
  BEGIN
    SELECT COUNT(*) INTO v_count
      FROM user_tab_columns
     WHERE table_name = 'PANTHEON_ROUTING_AUDIT'
       AND column_name = UPPER(p_col);
    IF v_count = 0 THEN
      EXECUTE IMMEDIATE 'ALTER TABLE pantheon_routing_audit ADD (' || p_ddl || ')';
    END IF;
  END;
BEGIN
  add_col('request_id',     'request_id VARCHAR2(256)');
  add_col('tenant_user_id', 'tenant_user_id VARCHAR2(256)');
  add_col('alias_or_model', 'alias_or_model VARCHAR2(256)');
  add_col('resolved_to',    'resolved_to VARCHAR2(256)');
  add_col('outcome',        'outcome VARCHAR2(64)');
  add_col('latency_ms',     'latency_ms NUMBER(12)');
  add_col('tokens_in',      'tokens_in NUMBER(12)');
  add_col('tokens_out',     'tokens_out NUMBER(12)');
  add_col('cost_usd',       'cost_usd NUMBER(10,4)');
  add_col('error_class',    'error_class VARCHAR2(256)');
  add_col('payload',        'payload CLOB');
  add_col('created',        'created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP');
END;
/

CREATE OR REPLACE TRIGGER pra_bi_id
BEFORE INSERT ON pantheon_routing_audit
FOR EACH ROW
WHEN (NEW.id IS NULL)
BEGIN
  :NEW.id := LOWER(RAWTOHEX(SYS_GUID()));
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_pra_created_desc ON pantheon_routing_audit (created DESC)';
EXCEPTION WHEN OTHERS THEN IF SQLCODE = -955 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_pra_tenant_created ON pantheon_routing_audit (tenant_user_id, created DESC)';
EXCEPTION WHEN OTHERS THEN IF SQLCODE = -955 THEN NULL; ELSE RAISE; END IF;
END;
/
