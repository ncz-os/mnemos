-- migration: 0026_hive_worker_kind_stats
-- target:    IBM Db2 12.1.x
-- purpose:   Per-worker per-kind aggregate counters. Mirrors Oracle
--            variant (db/migrations_oracle/0026_hive_worker_kind_stats.sql).

CREATE TABLE hive_worker_kind_stats (
  urn                 VARCHAR(256) NOT NULL,
  kind                VARCHAR(256) NOT NULL,
  success_count       BIGINT       NOT NULL WITH DEFAULT 0,
  fail_count          BIGINT       NOT NULL WITH DEFAULT 0,
  cancelled_count     BIGINT       NOT NULL WITH DEFAULT 0,
  total_tokens_in     BIGINT       NOT NULL WITH DEFAULT 0,
  total_tokens_out    BIGINT       NOT NULL WITH DEFAULT 0,
  total_cost_usd      DECIMAL(15,6) NOT NULL WITH DEFAULT 0,
  total_duration_sec  DECIMAL(15,3) NOT NULL WITH DEFAULT 0,
  last_run            DOUBLE,
  CONSTRAINT pk_hive_wkstats PRIMARY KEY (urn, kind)
);

CREATE INDEX ix_hive_wkstats_kind     ON hive_worker_kind_stats(kind);
CREATE INDEX ix_hive_wkstats_last_run ON hive_worker_kind_stats(last_run);
