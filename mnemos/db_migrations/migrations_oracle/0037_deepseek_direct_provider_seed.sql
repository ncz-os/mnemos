-- migration: 0037_deepseek_direct_provider_seed
-- Seed DeepSeek direct models and remove parity_postgres test residue.
--
-- model_registry is created by the standalone db/migrations_model_registry.sql
-- (PostgreSQL syntax) which ensure_oracle_schema() does NOT apply — the Oracle
-- numbered suite only globs migrations_oracle/*.sql. So on a clean Oracle
-- deploy the table is absent here and the seed below (and 0044's pricing
-- ALTERs) fail with ORA-00942. This guarded CREATE makes the Oracle suite
-- self-contained: it provisions the canonical model_registry shape (mirrors
-- migrations_model_registry.sql; Oracle 23ai types, NUMBER(1) booleans,
-- CLOB-IS-JSON for capabilities/raw_payload) only when no table exists yet.
-- Idempotent (guarded by USER_TABLES); pricing columns are added by 0044.
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'MODEL_REGISTRY';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE model_registry (
        -- id is VARCHAR2(100): upsert_model() writes f"{provider}:{model_id}"[:100]
        -- (oracle.py), so a 36-char GUID width would raise ORA-12899 on sync.
        id                   VARCHAR2(100)  DEFAULT LOWER(RAWTOHEX(SYS_GUID())) NOT NULL,
        provider             VARCHAR2(50)   NOT NULL,
        model_id             VARCHAR2(400)  NOT NULL,
        display_name         VARCHAR2(400),
        family               VARCHAR2(200),
        context_window       NUMBER(12),
        max_output_tokens    NUMBER(12),
        capabilities         CLOB           CHECK (capabilities IS JSON),
        input_cost_per_mtok  NUMBER(12, 6)  DEFAULT 0,
        output_cost_per_mtok NUMBER(12, 6)  DEFAULT 0,
        cache_read_per_mtok  NUMBER(12, 6)  DEFAULT 0,
        cache_write_per_mtok NUMBER(12, 6)  DEFAULT 0,
        available            NUMBER(1)      DEFAULT 1 NOT NULL,
        deprecated           NUMBER(1)      DEFAULT 0 NOT NULL,
        arena_score          NUMBER(8, 2),
        arena_rank           NUMBER(12),
        graeae_weight        NUMBER(5, 4),
        first_seen           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        last_seen            TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        last_synced          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        raw_payload          CLOB           CHECK (raw_payload IS JSON),
        CONSTRAINT pk_model_registry PRIMARY KEY (id),
        CONSTRAINT uq_model_registry_provider_model UNIQUE (provider, model_id)
      )
    ]';
  END IF;
END;
/

-- Reconcile a pre-existing MODEL_REGISTRY whose id is narrower than 100 (an
-- earlier shape used VARCHAR2(36)). upsert_model() writes a key up to 100 chars,
-- so widen in place — increasing a VARCHAR2 length is lossless, even on a PK.
DECLARE
  v_len NUMBER;
BEGIN
  SELECT data_length INTO v_len FROM user_tab_columns
   WHERE table_name = 'MODEL_REGISTRY' AND column_name = 'ID';
  IF v_len < 100 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry MODIFY (id VARCHAR2(100))';
  END IF;
EXCEPTION WHEN NO_DATA_FOUND THEN NULL;  -- column absent; CREATE above provisions it
END;
/

-- model_registry_sync_log shares the standalone model_registry schema and is
-- likewise never applied by ensure_oracle_schema. write_model_sync_log()
-- (oracle.py) inserts into it after every provider sync — without this guarded
-- CREATE a sync would upsert models then fail the log write with ORA-00942.
-- id is RAW(16): write_model_sync_log binds uuid.uuid4().bytes.
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'MODEL_REGISTRY_SYNC_LOG';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE model_registry_sync_log (
        id                RAW(16)        DEFAULT SYS_GUID() NOT NULL,
        provider          VARCHAR2(50)   NOT NULL,
        synced_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        models_found      NUMBER(12)     DEFAULT 0 NOT NULL,
        models_added      NUMBER(12)     DEFAULT 0 NOT NULL,
        models_updated    NUMBER(12)     DEFAULT 0 NOT NULL,
        models_deprecated NUMBER(12)     DEFAULT 0 NOT NULL,
        error             VARCHAR2(4000),
        CONSTRAINT pk_model_registry_sync_log PRIMARY KEY (id)
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
  create_index('IX_MODEL_REGISTRY_SYNC_PROVIDER',
               'CREATE INDEX ix_model_registry_sync_provider ON model_registry_sync_log(provider)');
  create_index('IX_MODEL_REGISTRY_SYNC_SYNCED_AT',
               'CREATE INDEX ix_model_registry_sync_synced_at ON model_registry_sync_log(synced_at DESC)');
END;
/

DELETE FROM model_registry
WHERE provider LIKE 'parity_postgres_%';

MERGE INTO model_registry dst
USING (
  SELECT 'deepseek-direct' provider,
         'deepseek-v4-flash' model_id,
         'DeepSeek V4 Flash' display_name,
         'deepseek-v4' family,
         128000 context_window,
         8192 max_output_tokens,
         0.14 input_cost_per_mtok,
         0.28 output_cost_per_mtok,
         0.0028 cache_read_per_mtok,
         '{"chat":true,"coding":true,"reasoning":false}' capabilities,
         1 available,
         0 deprecated,
         CAST(NULL AS NUMBER) arena_score,
         CAST(NULL AS NUMBER) arena_rank,
         0.7 graeae_weight,
         '{"source":"0037_deepseek_direct_provider_seed"}' raw_payload
  FROM dual
  UNION ALL
  SELECT 'deepseek-direct',
         'deepseek-v4-pro',
         'DeepSeek V4 Pro',
         'deepseek-v4',
         128000,
         8192,
         0.435,
         0.87,
         0.003625,
         '{"chat":true,"coding":true,"reasoning":true,"promo_until":"2026-05-31","post_promo_input_cost_per_mtok":1.74,"post_promo_output_cost_per_mtok":3.48}',
         1,
         0,
         CAST(NULL AS NUMBER),
         CAST(NULL AS NUMBER),
         0.85,
         '{"source":"0037_deepseek_direct_provider_seed","pricing_note":"DeepSeek V4 Pro promo pricing effective until 2026-05-31; then input/output costs revert to 1.74/3.48 USD per mtok."}'
  FROM dual
) src
ON (dst.provider = src.provider AND dst.model_id = src.model_id)
WHEN MATCHED THEN UPDATE SET
  dst.display_name = src.display_name,
  dst.family = src.family,
  dst.context_window = src.context_window,
  dst.max_output_tokens = src.max_output_tokens,
  dst.input_cost_per_mtok = src.input_cost_per_mtok,
  dst.output_cost_per_mtok = src.output_cost_per_mtok,
  dst.cache_read_per_mtok = src.cache_read_per_mtok,
  dst.capabilities = src.capabilities,
  dst.available = src.available,
  dst.deprecated = src.deprecated,
  dst.arena_score = src.arena_score,
  dst.arena_rank = src.arena_rank,
  dst.graeae_weight = src.graeae_weight,
  dst.last_seen = SYSTIMESTAMP,
  dst.last_synced = SYSTIMESTAMP,
  dst.raw_payload = src.raw_payload
WHEN NOT MATCHED THEN INSERT (
  id, provider, model_id, display_name, family, context_window,
  max_output_tokens, input_cost_per_mtok, output_cost_per_mtok,
  cache_read_per_mtok, capabilities, available, deprecated, arena_score,
  arena_rank, graeae_weight, first_seen, last_seen, last_synced, raw_payload
) VALUES (
  LOWER(RAWTOHEX(SYS_GUID())), src.provider, src.model_id, src.display_name, src.family,
  src.context_window, src.max_output_tokens, src.input_cost_per_mtok,
  src.output_cost_per_mtok, src.cache_read_per_mtok, src.capabilities,
  src.available, src.deprecated, src.arena_score, src.arena_rank,
  src.graeae_weight, SYSTIMESTAMP, SYSTIMESTAMP, SYSTIMESTAMP,
  src.raw_payload
);
