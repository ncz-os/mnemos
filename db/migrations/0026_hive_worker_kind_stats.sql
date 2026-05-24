-- migration: 0026_hive_worker_kind_stats
-- target:    PostgreSQL 16
-- schema:    public
-- purpose:   Per-worker per-kind aggregate counters. PG variant of
--            db/migrations_oracle/0026_hive_worker_kind_stats.sql.

CREATE TABLE IF NOT EXISTS hive_worker_kind_stats (
  urn                 VARCHAR(256) NOT NULL,
  kind                VARCHAR(256) NOT NULL,
  success_count       BIGINT       NOT NULL DEFAULT 0,
  fail_count          BIGINT       NOT NULL DEFAULT 0,
  cancelled_count     BIGINT       NOT NULL DEFAULT 0,
  total_tokens_in     BIGINT       NOT NULL DEFAULT 0,
  total_tokens_out    BIGINT       NOT NULL DEFAULT 0,
  total_cost_usd      NUMERIC(15,6) NOT NULL DEFAULT 0,
  total_duration_sec  NUMERIC(15,3) NOT NULL DEFAULT 0,
  last_run            DOUBLE PRECISION,
  CONSTRAINT pk_hive_wkstats PRIMARY KEY (urn, kind)
);

CREATE INDEX IF NOT EXISTS ix_hive_wkstats_kind     ON hive_worker_kind_stats(kind);
CREATE INDEX IF NOT EXISTS ix_hive_wkstats_last_run ON hive_worker_kind_stats(last_run);
