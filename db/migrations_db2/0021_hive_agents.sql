-- migration: 0021_hive_agents
-- target:    IBM Db2 12.1.5 (Oracle Compat mode)
-- schema:    HIVE_MIND (or active session schema)
-- purpose:   GRAEAE Hive Mind agent registry (Db2 variant). Mirrors
--            db/migrations_oracle/0021_hive_agents.sql + db/migrations/0021_hive_agents.sql.
--
-- Notes:
--   - JSON via CLOB CHECK IS JSON FORMAT in Db2 12.1 (native JSON column
--     type exists in 12.1.5 EAP — switch when GA per v6.1 P0 #6).
--   - DOUBLE PRECISION semantics matched via DOUBLE (Db2).

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE hive_agents (
      urn                   VARCHAR(256)  NOT NULL,
      kind                  VARCHAR(64)   NOT NULL,
      host                  VARCHAR(128)  NOT NULL,
      session_id            VARCHAR(128)  NOT NULL,
      pid                   INTEGER,
      capabilities          CLOB(1M) INLINE LENGTH 4096,
      version               VARCHAR(64),
      started_at            DOUBLE NOT NULL,
      last_heartbeat        DOUBLE NOT NULL,
      status                VARCHAR(16)   NOT NULL,
      metadata              CLOB(1M) INLINE LENGTH 4096,
      runtime               VARCHAR(64),
      model                 VARCHAR(128),
      provider              VARCHAR(64),
      autonomy_level        VARCHAR(32),
      cost_tier             VARCHAR(2),
      current_load          VARCHAR(32),
      auth_method           VARCHAR(64),
      plan_cap_usd          DECIMAL(12, 4),
      plan_period_used_usd  DECIMAL(12, 4) DEFAULT 0,
      CONSTRAINT pk_hive_agents PRIMARY KEY (urn),
      CONSTRAINT ck_hive_agents_status
        CHECK (status IN (''online'',''idle'',''offline'',''error''))
    )';
END%

BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_agents_status    ON hive_agents(status)';
END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_agents_kind      ON hive_agents(kind)';
END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_agents_heartbeat ON hive_agents(kind, last_heartbeat DESC)';
END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_agents_runtime   ON hive_agents(runtime)';
END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_agents_provider  ON hive_agents(provider)';
END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_agents_cost_tier ON hive_agents(cost_tier)';
END%
