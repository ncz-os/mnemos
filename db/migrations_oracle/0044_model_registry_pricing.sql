-- 0044_model_registry_pricing.sql — Oracle 23ai
-- Mirrors db/migrations/0044_model_registry_pricing.sql (canonical PG).
-- Oracle ALTER TABLE ADD without IF NOT EXISTS — idempotent via PL/SQL block.

-- ── model_registry: add pricing columns ──────────────────────────────────────

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (price_in NUMBER(12,6) DEFAULT 0)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (price_out NUMBER(12,6) DEFAULT 0)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (price_cached NUMBER(12,6) DEFAULT 0)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (price_updated_at TIMESTAMP WITH TIME ZONE)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

COMMENT ON COLUMN model_registry.price_in          IS 'USD per million input tokens (source: llm_provider_registry.json cost_in_per_m)';
COMMENT ON COLUMN model_registry.price_out         IS 'USD per million output tokens (source: llm_provider_registry.json cost_out_per_m)';
COMMENT ON COLUMN model_registry.price_cached      IS 'USD per million cached-input tokens (source: llm_provider_registry.json cache_hit_in_per_m)';
COMMENT ON COLUMN model_registry.price_updated_at  IS 'Timestamp of last pricing ingest from llm_provider_registry.json';

-- ── price_history: pricing audit trail ───────────────────────────────────────

BEGIN
    EXECUTE IMMEDIATE '
        CREATE TABLE price_history (
            id              VARCHAR2(36) PRIMARY KEY,
            provider        VARCHAR2(50) NOT NULL,
            model_id        VARCHAR2(400) NOT NULL,
            price_in        NUMBER(12,6),
            price_out       NUMBER(12,6),
            price_cached    NUMBER(12,6),
            prices          CLOB CHECK (prices IS JSON),
            recorded_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
        )';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -955 THEN NULL; ELSE RAISE; END IF;
END;
/

CREATE INDEX IF NOT EXISTS idx_ph_provider_model ON price_history(provider, model_id);
CREATE INDEX IF NOT EXISTS idx_ph_recorded_at     ON price_history(recorded_at DESC);
