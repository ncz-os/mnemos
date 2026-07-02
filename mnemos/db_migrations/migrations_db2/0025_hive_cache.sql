-- migration: 0025_hive_cache
-- target:    IBM Db2 12.1.x (with Oracle Compatibility Mode currently;
--            native dialect port tracked in v6.1 roadmap #44)
-- schema:    (configured per deploy; matches mnemos schema target)
-- purpose:   Result cache keyed by canonical-prompt hash. Mirrors Oracle
--            variant (db/migrations_oracle/0025_hive_cache.sql).
--
-- Notes:
--   - VARCHAR FOR BIT DATA not used; cache_key is hex string from app side.
--   - SYSTEM_TIME columns deliberately omitted; cache is ephemeral by design.

CREATE TABLE hive_cache (
  cache_key        VARCHAR(128)  NOT NULL,
  result_json      CLOB(2M) INLINE LENGTH 4096
                                CHECK (SYSTOOLS.JSON2BSON(result_json) IS NOT NULL) NOT NULL,
  source_job_id    VARCHAR(64),
  result_mnemos_id VARCHAR(64),
  hit_count        BIGINT        NOT NULL WITH DEFAULT 0,
  cost_saved_usd   DECIMAL(12,6) NOT NULL WITH DEFAULT 0,
  model            VARCHAR(128),
  provider         VARCHAR(64),
  cached_at        DOUBLE        NOT NULL,
  last_hit_at      DOUBLE,
  CONSTRAINT pk_hive_cache PRIMARY KEY (cache_key)
);

CREATE INDEX ix_hive_cache_cached_at ON hive_cache(cached_at);
CREATE INDEX ix_hive_cache_last_hit  ON hive_cache(last_hit_at);
CREATE INDEX ix_hive_cache_provider  ON hive_cache(provider);
