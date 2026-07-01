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
-- Idempotency + drift reconciliation: an earlier migration
-- (0011_hive_mind_extended_columns) also creates HIVE_CACHE, but with a
-- different, now-superseded column set (value / stored_at / expires_at).
-- On a clean deploy 0011 runs first and wins the CREATE, so this
-- migration must NOT assume its own freshly-created shape: it (1) creates
-- the canonical table only when absent, (2) reconciles any missing
-- columns onto a pre-existing table via guarded additive ALTERs, and
-- (3) builds each index only when both the index is absent AND its
-- target column is present. This makes the migration self-healing across
-- fresh installs and any prior on-disk shape (replayed every startup).

-- (1) Create the canonical table only when no HIVE_CACHE exists yet.
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

-- (2) Reconcile column drift: when HIVE_CACHE pre-exists (0011 shape),
-- add any columns this migration's indexes/queries require. Added
-- nullable (no NOT NULL) so the ALTER is safe even when the table holds
-- rows — HIVE_CACHE is a disposable result cache with no ORM consumer.
DECLARE
  v_count NUMBER;
  PROCEDURE add_col(p_col VARCHAR2, p_ddl VARCHAR2) IS
  BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tab_columns
     WHERE table_name = 'HIVE_CACHE' AND column_name = UPPER(p_col);
    IF v_count = 0 THEN
      EXECUTE IMMEDIATE 'ALTER TABLE hive_cache ADD (' || p_ddl || ')';
    END IF;
  END;
BEGIN
  add_col('result_json',      'result_json JSON');
  add_col('source_job_id',    'source_job_id VARCHAR2(64)');
  add_col('result_mnemos_id', 'result_mnemos_id VARCHAR2(64)');
  add_col('hit_count',        'hit_count NUMBER(12) DEFAULT 0');
  add_col('cost_saved_usd',   'cost_saved_usd NUMBER(12,6) DEFAULT 0');
  add_col('model',            'model VARCHAR2(128)');
  add_col('provider',         'provider VARCHAR2(64)');
  add_col('cached_at',        'cached_at NUMBER');
  add_col('last_hit_at',      'last_hit_at NUMBER');
END;
/

-- (3) Indexes: create only when the index is absent AND its target
-- column exists (the column-existence guard is what prevents ORA-00904
-- when an older, narrower HIVE_CACHE shape is present).
DECLARE
  PROCEDURE create_index(p_name VARCHAR2, p_col VARCHAR2, p_ddl VARCHAR2) IS
    v_i NUMBER;
    v_c NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_i FROM user_indexes WHERE index_name = p_name;
    SELECT COUNT(*) INTO v_c FROM user_tab_columns
     WHERE table_name = 'HIVE_CACHE' AND column_name = UPPER(p_col);
    IF v_i = 0 AND v_c > 0 THEN EXECUTE IMMEDIATE p_ddl; END IF;
  END;
BEGIN
  create_index('IX_HIVE_CACHE_CACHED_AT', 'cached_at',
               'CREATE INDEX ix_hive_cache_cached_at ON hive_cache(cached_at)');
  create_index('IX_HIVE_CACHE_LAST_HIT', 'last_hit_at',
               'CREATE INDEX ix_hive_cache_last_hit ON hive_cache(last_hit_at)');
  create_index('IX_HIVE_CACHE_PROVIDER', 'provider',
               'CREATE INDEX ix_hive_cache_provider ON hive_cache(provider)');
END;
/

COMMIT;
