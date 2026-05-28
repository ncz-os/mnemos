-- migration: 0027_hive_scheduled_jobs
-- target:    PostgreSQL 16
-- schema:    public
-- purpose:   Recurring job templates. PG variant of
--            db/migrations_oracle/0027_hive_scheduled_jobs.sql.

CREATE TABLE IF NOT EXISTS hive_scheduled_jobs (
  id                VARCHAR(64)  NOT NULL,
  name              VARCHAR(256) NOT NULL,
  created_by_urn    VARCHAR(256) NOT NULL,
  interval_seconds  BIGINT       NOT NULL,
  job_template      JSONB        NOT NULL,
  enabled           SMALLINT     NOT NULL DEFAULT 1,
  last_fired_at     DOUBLE PRECISION,
  next_fire_at      DOUBLE PRECISION NOT NULL,
  fire_count        BIGINT       NOT NULL DEFAULT 0,
  created_at        DOUBLE PRECISION NOT NULL,
  CONSTRAINT pk_hive_scheduled PRIMARY KEY (id),
  CONSTRAINT ck_hive_scheduled_enabled CHECK (enabled IN (0,1))
);

CREATE INDEX IF NOT EXISTS ix_hive_scheduled_next    ON hive_scheduled_jobs(next_fire_at, enabled);
CREATE INDEX IF NOT EXISTS ix_hive_scheduled_creator ON hive_scheduled_jobs(created_by_urn);
