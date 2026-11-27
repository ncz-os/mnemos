-- 0073: ModelRegistryRepository phase 2 — Postgres physical shape.
--
-- Closes the five named Postgres gaps listed by
-- ``PostgresModelRegistryRepository`` (``upsert_price``,
-- ``upsert_benchmark``, ``curated_routing_get``, ``curated_routing_replace``,
-- ``health_write``). The shape is the SQLite mirror
-- (``migrations_sqlite/migrations_v7_1_model_registry_repository_sqlite.sql``)
-- translated to Postgres with the same nullable-price rule:
-- ``NULL`` represents an unknown price and ``0`` represents a free model.
--
-- Normalize legacy prices and backfill price and benchmark rows for models
-- that do not already have entries in the new tables.

BEGIN;

-- ── model_registry: ABC columns missing from the Postgres physical shape ────
-- price_* are nullable on purpose (NULL = unpriced). pricing_model / band have
-- defaults so every legacy row round-trips with the ABC's defaults.
ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS max_output_tokens INTEGER;
ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS price_in NUMERIC(12, 6);
ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS price_out NUMERIC(12, 6);
ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS price_cached NUMERIC(12, 6);
ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS price_updated_at TIMESTAMPTZ;
ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS pricing_model TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS band TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS alias_of TEXT;

-- Migration 0044 supplied zero defaults. Only timestamped legacy prices
-- represent an observation, including explicit zero prices.
ALTER TABLE model_registry
    ALTER COLUMN price_in DROP DEFAULT,
    ALTER COLUMN price_out DROP DEFAULT,
    ALTER COLUMN price_cached DROP DEFAULT;

UPDATE model_registry
SET price_in = NULL, price_out = NULL, price_cached = NULL
WHERE price_updated_at IS NULL
  AND (price_in IS NOT NULL OR price_out IS NOT NULL OR price_cached IS NOT NULL);

-- The (provider, model_id) primary key makes alias IDs unique per provider.
-- Multiple distinct aliases can point to the same target.
DROP INDEX IF EXISTS uq_model_registry_provider_alias;

-- ── model_prices: (model, pricing_model, band) rows ──────────────────────────
CREATE TABLE IF NOT EXISTS model_prices (
    provider       VARCHAR(50) NOT NULL,
    model_id       TEXT        NOT NULL,
    pricing_model  TEXT        NOT NULL DEFAULT 'unknown'
                   CHECK (pricing_model IN ('payg', 'subscription', 'free_tier', 'local', 'unknown')),
    band           TEXT        NOT NULL DEFAULT 'standard'
                   CHECK (band IN ('standard', 'peak', 'off_peak')),
    price_in       NUMERIC(12, 6),
    price_out      NUMERIC(12, 6),
    price_cached   NUMERIC(12, 6),
    observed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, model_id, pricing_model, band)
);

CREATE INDEX IF NOT EXISTS idx_model_prices_model
    ON model_prices (provider, model_id);

-- Copy timestamped legacy prices for canonical models without band rows.
-- Preserve the observation timestamp and each price, including NULL and zero.
INSERT INTO model_prices
    (provider, model_id, pricing_model, band, price_in, price_out, price_cached, observed_at)
SELECT m.provider, m.model_id, 'unknown', 'standard',
       m.price_in, m.price_out, m.price_cached, m.price_updated_at
FROM model_registry m
WHERE m.alias_of IS NULL
  AND m.price_updated_at IS NOT NULL
  AND (m.price_in IS NOT NULL OR m.price_out IS NOT NULL OR m.price_cached IS NOT NULL)
  AND NOT EXISTS (
      SELECT 1 FROM model_prices p
      WHERE p.provider = m.provider AND p.model_id = m.model_id
  )
ON CONFLICT (provider, model_id, pricing_model, band) DO NOTHING;

-- ── model_registry_field_obs: per-field value + provenance ───────────────────
-- One row per (provider, model_id, field); source/fetched_at belong to the
-- FIELD. ``record_field_obs`` / ``merge_fields`` already query this table;
-- no Postgres migration created it, so fresh clusters would fail the write.
-- Additive: CREATE TABLE IF NOT EXISTS, replay-safe.
CREATE TABLE IF NOT EXISTS model_registry_field_obs (
    provider    VARCHAR(50) NOT NULL,
    model_id    TEXT        NOT NULL,
    field       TEXT        NOT NULL,
    value       TEXT,
    source      TEXT        NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider, model_id, field)
);

CREATE INDEX IF NOT EXISTS idx_model_registry_field_obs_source
    ON model_registry_field_obs (source);

-- ── model_benchmarks: (model, benchmark) with provenance ────────────────────
CREATE TABLE IF NOT EXISTS model_benchmarks (
    provider    VARCHAR(50) NOT NULL,
    model_id    TEXT        NOT NULL,
    benchmark   TEXT        NOT NULL,
    score       NUMERIC(12, 6),
    source      TEXT        NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider, model_id, benchmark)
);

-- One-time backfill: the legacy arena score lives on the ``model_registry``
-- row (column present since 0037). Correlated ``NOT EXISTS`` keeps it
-- per-model (replay-safe and partial-run-safe) so ``export_snapshot`` does
-- not lose the score for clusters populated before this migration.
INSERT INTO model_benchmarks
    (provider, model_id, benchmark, score, source, fetched_at)
SELECT m.provider, m.model_id, 'arena_score', m.arena_score, 'model_registry_row', NOW()
FROM model_registry m
WHERE m.alias_of IS NULL
  AND m.arena_score IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM model_benchmarks b
      WHERE b.provider = m.provider AND b.model_id = m.model_id
  )
ON CONFLICT (provider, model_id, benchmark) DO NOTHING;

-- ── curated_routing: versioned, append-only ──────────────────────────────────
-- Version is the primary identity: ``curated_routing_replace`` bumps it and
-- never rewrites a prior version. The refresh path only ever SELECTs.
CREATE TABLE IF NOT EXISTS curated_routing (
    version     BIGINT      NOT NULL PRIMARY KEY,
    rules       JSONB       NOT NULL DEFAULT '[]',
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── model_registry_health: last health snapshot per backend ──────────────────
-- ``curated_version`` is BIGINT to match ``curated_routing.version``; replay
-- widens an existing narrower column instead of failing on a type mismatch.
CREATE TABLE IF NOT EXISTS model_registry_health (
    backend          VARCHAR(50) PRIMARY KEY,
    model_count      INTEGER     NOT NULL DEFAULT 0,
    priced_count     INTEGER     NOT NULL DEFAULT 0,
    unpriced_count   INTEGER     NOT NULL DEFAULT 0,
    curated_version  BIGINT      NOT NULL DEFAULT 0,
    last_sync_at     TIMESTAMPTZ,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE model_registry_health ALTER COLUMN curated_version TYPE BIGINT;

-- ── model_registry_sync_log: additive ABC counters (0049 parity) ────────────
-- The table exists with ``models_found`` ... ``duration_ms`` from 0049; the
-- guard keeps replay safe on clusters that only have the legacy shape.
ALTER TABLE model_registry_sync_log ADD COLUMN IF NOT EXISTS models_found INTEGER;
ALTER TABLE model_registry_sync_log ADD COLUMN IF NOT EXISTS models_added INTEGER;
ALTER TABLE model_registry_sync_log ADD COLUMN IF NOT EXISTS models_updated INTEGER;
ALTER TABLE model_registry_sync_log ADD COLUMN IF NOT EXISTS models_deprecated INTEGER;
ALTER TABLE model_registry_sync_log ADD COLUMN IF NOT EXISTS duration_ms INTEGER;

-- ── legacy curated_routing_versions guard ────────────────────────────────────
-- If an older cluster ever carried live curated versions in a separate
-- ``curated_routing_versions`` table, copy them into the authoritative
-- ``curated_routing`` table (skipping versions that already exist) so
-- ``health_read`` / ``curated_routing_get`` do not silently report version 0
-- after the migration. No-op when the old table does not exist.
DO $$
BEGIN
    IF to_regclass('curated_routing_versions') IS NOT NULL THEN
        EXECUTE format(
            'INSERT INTO curated_routing (version, rules, note, created_at)
             SELECT version, rules, note, created_at FROM curated_routing_versions
             ON CONFLICT (version) DO NOTHING'
        );
    END IF;
END $$;

COMMENT ON TABLE model_prices IS
    'Band-aware price rows for ModelRegistryRepository (provider, model_id, pricing_model, band).';
COMMENT ON TABLE model_benchmarks IS
    'Benchmark scores with per-row provenance for ModelRegistryRepository.';
COMMENT ON TABLE curated_routing IS
    'Versioned, append-only curated routing layer owned by operators.';
COMMENT ON TABLE model_registry_health IS
    'Last health snapshot written by ModelRegistryRepository.health_write.';

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON model_registry_field_obs TO mnemos_user;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON model_registry_field_obs TO mnemos;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON model_prices TO mnemos_user;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON model_prices TO mnemos;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON model_benchmarks TO mnemos_user;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON model_benchmarks TO mnemos;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON curated_routing TO mnemos_user;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON curated_routing TO mnemos;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON model_registry_health TO mnemos_user;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON model_registry_health TO mnemos;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

COMMIT;
