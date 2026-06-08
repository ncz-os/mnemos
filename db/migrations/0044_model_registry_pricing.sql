-- 0044_model_registry_pricing.sql
-- Extend model_registry with price_in/price_out/price_cached columns sourced
-- from llm_provider_registry.json (cost_in_per_m, cost_out_per_m, cache_hit_in_per_m).
-- Adds price_history table for audit trail of pricing changes.
-- KNEMON MVP Step 2: groq/xai tier pricing ingest.

-- ── model_registry: add pricing columns ──────────────────────────────────────

ALTER TABLE model_registry
    ADD COLUMN IF NOT EXISTS price_in          NUMERIC(12, 6) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS price_out         NUMERIC(12, 6) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS price_cached      NUMERIC(12, 6) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS price_updated_at  TIMESTAMPTZ;

COMMENT ON COLUMN model_registry.price_in          IS 'USD per million input tokens (source: llm_provider_registry.json cost_in_per_m)';
COMMENT ON COLUMN model_registry.price_out         IS 'USD per million output tokens (source: llm_provider_registry.json cost_out_per_m)';
COMMENT ON COLUMN model_registry.price_cached      IS 'USD per million cached-input tokens (source: llm_provider_registry.json cache_hit_in_per_m)';
COMMENT ON COLUMN model_registry.price_updated_at  IS 'Timestamp of last pricing ingest from llm_provider_registry.json';

-- ── price_history: pricing audit trail ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS price_history (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        VARCHAR(50) NOT NULL,
    model_id        TEXT        NOT NULL,
    price_in        NUMERIC(12, 6),
    price_out       NUMERIC(12, 6),
    price_cached    NUMERIC(12, 6),
    prices          JSONB       DEFAULT '{}',
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_history_provider_model
    ON price_history(provider, model_id);

CREATE INDEX IF NOT EXISTS idx_price_history_recorded_at
    ON price_history(recorded_at DESC);
