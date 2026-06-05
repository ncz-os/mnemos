-- 0041_model_registry_pricing.sql — IBM Db2 12.1 (Oracle Compat mode)
-- Mirrors db/migrations/0041_model_registry_pricing.sql (canonical PG).
-- Db2 Oracle-compat: ADD COLUMN is idempotent via continue handler.

-- ── model_registry: add pricing columns ──────────────────────────────────────

BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_in DECIMAL(12,6) DEFAULT 0';
END
/

BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_out DECIMAL(12,6) DEFAULT 0';
END
/

BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_cached DECIMAL(12,6) DEFAULT 0';
END
/

BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_updated_at TIMESTAMP';
END
/

COMMENT ON COLUMN model_registry.price_in          IS 'USD per million input tokens (source: llm_provider_registry.json cost_in_per_m)';
COMMENT ON COLUMN model_registry.price_out         IS 'USD per million output tokens (source: llm_provider_registry.json cost_out_per_m)';
COMMENT ON COLUMN model_registry.price_cached      IS 'USD per million cached-input tokens (source: llm_provider_registry.json cache_hit_in_per_m)';
COMMENT ON COLUMN model_registry.price_updated_at  IS 'Timestamp of last pricing ingest from llm_provider_registry.json';

-- ── price_history: pricing audit trail ───────────────────────────────────────

BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
    EXECUTE IMMEDIATE '
        CREATE TABLE price_history (
            id              VARCHAR(36) NOT NULL PRIMARY KEY,
            provider        VARCHAR(50) NOT NULL,
            model_id        VARCHAR(400) NOT NULL,
            price_in        DECIMAL(12,6),
            price_out       DECIMAL(12,6),
            price_cached    DECIMAL(12,6),
            prices          CLOB CHECK (prices IS JSON),
            recorded_at     TIMESTAMP NOT NULL DEFAULT CURRENT TIMESTAMP
        )';
END
/

CREATE INDEX IF NOT EXISTS idx_ph_provider_model ON price_history(provider, model_id);
CREATE INDEX IF NOT EXISTS idx_ph_recorded_at     ON price_history(recorded_at DESC);
