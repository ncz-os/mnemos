-- migration: 0023_hive_messages
-- target:    PostgreSQL 16
-- schema:    public
-- purpose:   Hive Mind agent-to-agent messages (PG variant).

CREATE TABLE IF NOT EXISTS hive_messages (
  id           VARCHAR(64)   NOT NULL,
  from_urn     VARCHAR(256)  NOT NULL,
  to_urn       VARCHAR(256),
  in_reply_to  VARCHAR(64),
  topic        VARCHAR(128)  NOT NULL,
  payload      JSONB         NOT NULL,
  ts           DOUBLE PRECISION NOT NULL,
  CONSTRAINT pk_hive_messages PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_hive_messages_to    ON hive_messages(to_urn);
CREATE INDEX IF NOT EXISTS ix_hive_messages_topic ON hive_messages(topic);
CREATE INDEX IF NOT EXISTS ix_hive_messages_ts    ON hive_messages(ts DESC);
