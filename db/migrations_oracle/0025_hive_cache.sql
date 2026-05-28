-- migration: 0025_hive_cache
-- target:    Oracle 23ai PDB ORCLPDB1 (PYTHIA + CERBERUS standby)
-- schema:    HIVE_MIND
-- purpose:   Result cache keyed by canonical-prompt hash. Lets the dispatcher
--            skip identical work that another agent already paid for.
--            Mirrors /srv/agent-bus/agents.db `hive_cache` SQLite table.
--
-- Notes:
--   - cache_key is caller-canonical hash (SHA-256 hex over normalized prompt
--     + model + provider + cost_tier). Up to 128 chars accommodates either
--     hex SHA-256 (64) or longer composite keys.
--   - result_json native Oracle 23ai JSON for fast SQL/JSON path queries.
--   - cost_saved_usd tallies cumulative savings across hits (informational).
--   - source_job_id + result_mnemos_id back-references for audit.
--   - hit_count + last_hit_at drive cache eviction policy (LRU + age).
--
-- Upsert pattern: MERGE INTO hive_cache USING (SELECT ... FROM dual) ... .
--
-- Idempotency: guarded by USER_TABLES.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'HIVE_CACHE';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hive_cache (
        cache_key        VARCHAR2(128)  NOT NULL,
        result_json      JSON           NOT NULL,
        source_job_id    VARCHAR2(64),
        result_mnemos_id VARCHAR2(64),
        hit_count        NUMBER(12)     DEFAULT 0 NOT NULL,
        cost_saved_usd   NUMBER(12, 6)  DEFAULT 0 NOT NULL,
        model            VARCHAR2(128),
        provider         VARCHAR2(64),
        cached_at        NUMBER         NOT NULL,
        last_hit_at      NUMBER,
        CONSTRAINT pk_hive_cache PRIMARY KEY (cache_key)
      )
    ]';
  END IF;
END;
/

DECLARE
  PROCEDURE create_index(p_name VARCHAR2, p_ddl VARCHAR2) IS
    v_n NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_n FROM user_indexes WHERE index_name = p_name;
    IF v_n = 0 THEN EXECUTE IMMEDIATE p_ddl; END IF;
  END;
BEGIN
  create_index('IX_HIVE_CACHE_CACHED_AT',
               'CREATE INDEX ix_hive_cache_cached_at ON hive_cache(cached_at)');
  create_index('IX_HIVE_CACHE_LAST_HIT',
               'CREATE INDEX ix_hive_cache_last_hit ON hive_cache(last_hit_at)');
  create_index('IX_HIVE_CACHE_PROVIDER',
               'CREATE INDEX ix_hive_cache_provider ON hive_cache(provider)');
END;
/

COMMIT;
