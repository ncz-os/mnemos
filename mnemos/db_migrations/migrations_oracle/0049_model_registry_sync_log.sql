-- Parity restatement. Oracle already creates MODEL_REGISTRY_SYNC_LOG in
-- 0037_deepseek_direct_provider_seed.sql; ensure_oracle_schema never applies the
-- standalone model_registry schema, so that guarded CREATE is the real one. The
-- parity gate wants this basename present for all three backends, so this file
-- restates the same end state under the same existence guard. No-op on any
-- database that ran 0037.
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
        error             CLOB,
        duration_ms       NUMBER(12),
        CONSTRAINT pk_model_registry_sync_log PRIMARY KEY (id)
      )]';
  END IF;
END;
/
