-- migration: 0027_hive_scheduled_jobs
-- target:    IBM Db2 12.1.x
-- purpose:   Recurring job templates. Mirrors Oracle variant
--            (db/migrations_oracle/0027_hive_scheduled_jobs.sql).

CREATE TABLE hive_scheduled_jobs (
  id                VARCHAR(64)  NOT NULL,
  name              VARCHAR(256) NOT NULL,
  created_by_urn    VARCHAR(256) NOT NULL,
  interval_seconds  BIGINT       NOT NULL,
  job_template      CLOB(2M) INLINE LENGTH 4096 NOT NULL,
  enabled           SMALLINT     NOT NULL WITH DEFAULT 1,
  last_fired_at     DOUBLE,
  next_fire_at      DOUBLE       NOT NULL,
  fire_count        BIGINT       NOT NULL WITH DEFAULT 0,
  created_at        DOUBLE       NOT NULL,
  CONSTRAINT pk_hive_scheduled PRIMARY KEY (id),
  CONSTRAINT ck_hive_scheduled_enabled CHECK (enabled IN (0,1))
);

CREATE INDEX ix_hive_scheduled_next    ON hive_scheduled_jobs(next_fire_at, enabled);
CREATE INDEX ix_hive_scheduled_creator ON hive_scheduled_jobs(created_by_urn);
