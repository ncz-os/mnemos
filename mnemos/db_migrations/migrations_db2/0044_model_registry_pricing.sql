--#SET TERMINATOR @
-- 0044_model_registry_pricing.sql — IBM Db2 12.1.5 (native dialect)
-- Mirrors db/migrations/0044_model_registry_pricing.sql (canonical PG).
-- Db2 native: ADD COLUMN idempotent via SQLSTATE 42711 continue handler; CREATE
-- idempotent via 42710. No PG-style IF NOT EXISTS (Db2 rejects it) and no
-- Oracle-style `/` terminator (the runner splits on `@`).

-- ── model_registry: add pricing columns ──────────────────────────────────────
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_in DECIMAL(12,6) DEFAULT 0';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_out DECIMAL(12,6) DEFAULT 0';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_cached DECIMAL(12,6) DEFAULT 0';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_updated_at TIMESTAMP(6)';
END@

COMMENT ON COLUMN model_registry.price_in         IS 'USD per million input tokens (source: llm_provider_registry.json cost_in_per_m)'@
COMMENT ON COLUMN model_registry.price_out        IS 'USD per million output tokens (source: llm_provider_registry.json cost_out_per_m)'@
COMMENT ON COLUMN model_registry.price_cached     IS 'USD per million cached-input tokens (source: llm_provider_registry.json cache_hit_in_per_m)'@
COMMENT ON COLUMN model_registry.price_updated_at IS 'Timestamp of last pricing ingest from llm_provider_registry.json'@

-- ── price_history: pricing audit trail ───────────────────────────────────────
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE price_history (
      id              VARCHAR(36)  NOT NULL PRIMARY KEY,
      provider        VARCHAR(50)  NOT NULL,
      model_id        VARCHAR(400) NOT NULL,
      price_in        DECIMAL(12,6),
      price_out       DECIMAL(12,6),
      price_cached    DECIMAL(12,6),
      prices          CLOB(1M) INLINE LENGTH 1024
        CHECK (prices IS NULL OR SYSTOOLS.JSON2BSON(prices) IS NOT NULL),
      recorded_at     TIMESTAMP(6) NOT NULL DEFAULT CURRENT TIMESTAMP
    )';
END@

-- Db2 has no CREATE INDEX IF NOT EXISTS; make each idempotent via 42710.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_ph_provider_model ON price_history (provider, model_id)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_ph_recorded_at ON price_history (recorded_at DESC)';
END@
