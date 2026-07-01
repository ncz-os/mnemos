-- migration: 0024_hive_events
-- target:    PostgreSQL 16
-- schema:    public
-- purpose:   Hive Mind event audit log (PG variant).

CREATE TABLE IF NOT EXISTS hive_events (
  id         BIGSERIAL       NOT NULL,
  ts         DOUBLE PRECISION NOT NULL,
  kind       VARCHAR(64)     NOT NULL,
  payload    JSONB           NOT NULL,
  agent_urn  VARCHAR(256),
  CONSTRAINT pk_hive_events PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_hive_events_ts      ON hive_events(ts DESC);
CREATE INDEX IF NOT EXISTS ix_hive_events_kind    ON hive_events(kind);
CREATE INDEX IF NOT EXISTS ix_hive_events_agent   ON hive_events(agent_urn);
CREATE INDEX IF NOT EXISTS ix_hive_events_kind_ts ON hive_events(kind, ts DESC);
