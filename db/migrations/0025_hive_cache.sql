-- migration: 0025_hive_cache
-- target:    PostgreSQL 16
-- schema:    public
-- purpose:   Result cache keyed by canonical-prompt hash. Mirrors Oracle
--            variant (db/migrations_oracle/0025_hive_cache.sql) with
--            PG-native types (JSONB, BIGINT, DOUBLE PRECISION epoch).

CREATE TABLE IF NOT EXISTS hive_cache (
  cache_key        VARCHAR(128)  NOT NULL,
  result_json      JSONB         NOT NULL,
  source_job_id    VARCHAR(64),
  result_mnemos_id VARCHAR(64),
  hit_count        BIGINT        NOT NULL DEFAULT 0,
  cost_saved_usd   NUMERIC(12,6) NOT NULL DEFAULT 0,
  model            VARCHAR(128),
  provider         VARCHAR(64),
  cached_at        DOUBLE PRECISION NOT NULL,
  last_hit_at      DOUBLE PRECISION,
  CONSTRAINT pk_hive_cache PRIMARY KEY (cache_key)
);

CREATE INDEX IF NOT EXISTS ix_hive_cache_cached_at ON hive_cache(cached_at);
CREATE INDEX IF NOT EXISTS ix_hive_cache_last_hit  ON hive_cache(last_hit_at);
CREATE INDEX IF NOT EXISTS ix_hive_cache_provider  ON hive_cache(provider);
