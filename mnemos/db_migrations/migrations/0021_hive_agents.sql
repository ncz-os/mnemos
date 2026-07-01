-- migration: 0021_hive_agents
-- target:    PostgreSQL 16 + pgvector (development + cixmini edge)
-- schema:    public (or HIVE_MIND if separate schema configured)
-- purpose:   GRAEAE Hive Mind agent registry — Phase 2 SQLite -> PG port.
--            Mirrors Oracle 23ai version (db/migrations_oracle/0021_hive_agents.sql)
--            with PG-native types (JSONB, BIGINT) and identical column names.

CREATE TABLE IF NOT EXISTS hive_agents (
  urn                   VARCHAR(256)  NOT NULL,
  kind                  VARCHAR(64)   NOT NULL,
  host                  VARCHAR(128)  NOT NULL,
  session_id            VARCHAR(128)  NOT NULL,
  pid                   INTEGER,
  capabilities          JSONB,
  version               VARCHAR(64),
  started_at            DOUBLE PRECISION NOT NULL,
  last_heartbeat        DOUBLE PRECISION NOT NULL,
  status                VARCHAR(16)   NOT NULL,
  metadata              JSONB,
  runtime               VARCHAR(64),
  model                 VARCHAR(128),
  provider              VARCHAR(64),
  autonomy_level        VARCHAR(32),
  cost_tier             VARCHAR(2),
  current_load          VARCHAR(32),
  auth_method           VARCHAR(64),
  plan_cap_usd          NUMERIC(12, 4),
  plan_period_used_usd  NUMERIC(12, 4) DEFAULT 0,
  CONSTRAINT pk_hive_agents PRIMARY KEY (urn),
  CONSTRAINT ck_hive_agents_status
    CHECK (status IN ('online','idle','offline','error'))
);

CREATE INDEX IF NOT EXISTS ix_hive_agents_status    ON hive_agents(status);
CREATE INDEX IF NOT EXISTS ix_hive_agents_kind      ON hive_agents(kind);
CREATE INDEX IF NOT EXISTS ix_hive_agents_heartbeat ON hive_agents(kind, last_heartbeat DESC);
CREATE INDEX IF NOT EXISTS ix_hive_agents_runtime   ON hive_agents(runtime);
CREATE INDEX IF NOT EXISTS ix_hive_agents_provider  ON hive_agents(provider);
CREATE INDEX IF NOT EXISTS ix_hive_agents_cost_tier ON hive_agents(cost_tier);
