-- migration: 0001_hive_agents
-- target:    Oracle 23ai PDB ORCLPDB1 (PYTHIA + CERBERUS standby)
-- schema:    HIVE_MIND
-- purpose:   GRAEAE Hive Mind agent registry — Phase 2 SQLite -> Oracle port.
--            Mirrors /srv/agent-bus/agents.db SQLite agents table with
--            Oracle 23ai native JSON, identity column, and CHECK constraints.
--
-- Notes:
--   - urn is opaque string (urn:agent:<kind>:<host>:<uuid>) up to 256 chars.
--   - capabilities + metadata stored as JSON (Oracle 23ai native).
--   - last_heartbeat is NUMBER (epoch seconds, matches Python time.time()).
--   - All extension columns (runtime/model/provider/cost_tier/etc) included
--     so the SQLite snapshot can be bulk-inserted without schema delta.
--
-- Idempotency: guarded by USER_TABLES check.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'HIVE_AGENTS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hive_agents (
        urn                   VARCHAR2(256)  NOT NULL,
        kind                  VARCHAR2(64)   NOT NULL,
        host                  VARCHAR2(128)  NOT NULL,
        session_id            VARCHAR2(128)  NOT NULL,
        pid                   NUMBER(10),
        capabilities          JSON,
        version               VARCHAR2(64),
        started_at            NUMBER         NOT NULL,
        last_heartbeat        NUMBER         NOT NULL,
        status                VARCHAR2(16)   NOT NULL,
        metadata              JSON,
        runtime               VARCHAR2(64),
        model                 VARCHAR2(128),
        provider              VARCHAR2(64),
        autonomy_level        VARCHAR2(32),
        cost_tier             VARCHAR2(2),
        current_load          VARCHAR2(32),
        auth_method           VARCHAR2(64),
        plan_cap_usd          NUMBER(12, 4),
        plan_period_used_usd  NUMBER(12, 4)  DEFAULT 0,
        CONSTRAINT pk_hive_agents PRIMARY KEY (urn),
        CONSTRAINT ck_hive_agents_status
          CHECK (status IN ('online','idle','offline','error'))
      )
    ]';
  END IF;
END;
/

DECLARE
  v_count NUMBER;
  PROCEDURE create_index(p_name VARCHAR2, p_ddl VARCHAR2) IS
    v_n NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_n FROM user_indexes WHERE index_name = p_name;
    IF v_n = 0 THEN EXECUTE IMMEDIATE p_ddl; END IF;
  END;
BEGIN
  create_index('IX_HIVE_AGENTS_STATUS',     'CREATE INDEX ix_hive_agents_status ON hive_agents(status)');
  create_index('IX_HIVE_AGENTS_KIND',       'CREATE INDEX ix_hive_agents_kind ON hive_agents(kind)');
  create_index('IX_HIVE_AGENTS_HEARTBEAT',  'CREATE INDEX ix_hive_agents_heartbeat ON hive_agents(kind, last_heartbeat DESC)');
  create_index('IX_HIVE_AGENTS_RUNTIME',    'CREATE INDEX ix_hive_agents_runtime ON hive_agents(runtime)');
  create_index('IX_HIVE_AGENTS_PROVIDER',   'CREATE INDEX ix_hive_agents_provider ON hive_agents(provider)');
  create_index('IX_HIVE_AGENTS_COST_TIER',  'CREATE INDEX ix_hive_agents_cost_tier ON hive_agents(cost_tier)');
END;
/

COMMIT;
