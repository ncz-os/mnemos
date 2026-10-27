-- migrations_v7_1_model_registry_repository_sqlite.sql
-- ModelRegistryRepository (provider/pricing DB phase 1.5) — SQLite mirror.
--
-- SQLite is the CI target for the model-registry ABC. The Oracle/Postgres
-- physical schema is NOT touched here (no Oracle DDL, no live data moves);
-- this only provisions the tables the SQLite implementation of
-- mnemos.persistence.base.ModelRegistryRepository owns.
--
-- NULL means unpriced. Every price column is nullable with NO DEFAULT: a
-- missing price is NULL, and 0.0 is reserved for "demonstrably free".
-- SQLite's ALTER TABLE cannot add a NOT NULL column without a default, so
-- the pre-existing model_registry price columns
-- (input_cost_per_mtok/output_cost_per_mtok) are left exactly as they are;
-- the repository reads/writes price_in/price_out/price_cached/price_cached
-- below, which are added nullable on purpose.

-- ── model_registry: ABC columns ──────────────────────────────────────────────
-- price_* are nullable on purpose (NULL = unpriced). max_output_tokens,
-- arena_rank and price_updated_at were missing from the SQLite shape even
-- though Postgres/Oracle have them; adding them here keeps the row contract
-- backend-neutral without touching any other backend.
ALTER TABLE model_registry ADD COLUMN max_output_tokens INTEGER;
ALTER TABLE model_registry ADD COLUMN arena_rank INTEGER;
ALTER TABLE model_registry ADD COLUMN price_in REAL;
ALTER TABLE model_registry ADD COLUMN price_out REAL;
ALTER TABLE model_registry ADD COLUMN price_cached REAL;
ALTER TABLE model_registry ADD COLUMN price_updated_at TEXT;
ALTER TABLE model_registry ADD COLUMN pricing_model TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE model_registry ADD COLUMN band TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE model_registry ADD COLUMN alias_of TEXT;

CREATE INDEX IF NOT EXISTS idx_model_registry_alias
  ON model_registry(provider, alias_of);

-- An alias is unique per provider and may never be its own target. Lookalike
-- rejection (case/separator twins of a real id) is enforced in the repository,
-- because SQLite has no NOCASE collation on the canonical table.
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_registry_provider_alias
  ON model_registry(provider, alias_of)
  WHERE alias_of IS NOT NULL AND alias_of <> model_id;

-- ── model_prices: (model, pricing_model, band) rows ───────────────────────────
CREATE TABLE IF NOT EXISTS model_prices (
  provider       TEXT    NOT NULL,
  model_id       TEXT    NOT NULL,
  pricing_model  TEXT    NOT NULL DEFAULT 'unknown'
                 CHECK (pricing_model IN ('payg', 'subscription', 'free_tier', 'local', 'unknown')),
  band           TEXT    NOT NULL DEFAULT 'standard'
                 CHECK (band IN ('standard', 'peak', 'off_peak')),
  price_in       REAL,
  price_out      REAL,
  price_cached   REAL,
  observed_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (provider, model_id, pricing_model, band)
);

CREATE INDEX IF NOT EXISTS idx_model_prices_model
  ON model_prices(provider, model_id);

-- ── model_field_obs: per-field value + provenance ─────────────────────────────
-- One row per (provider, model_id, field). The source and fetched_at belong
-- to the FIELD, not the row, so refreshing context_window never restamps the
-- price observation as fresh.
CREATE TABLE IF NOT EXISTS model_field_obs (
  provider    TEXT NOT NULL,
  model_id    TEXT NOT NULL,
  field       TEXT NOT NULL,
  value       TEXT,
  source      TEXT NOT NULL,
  fetched_at  TEXT NOT NULL,
  PRIMARY KEY (provider, model_id, field)
);

CREATE INDEX IF NOT EXISTS idx_model_field_obs_source
  ON model_field_obs(source);

-- ── model_benchmarks: (model, benchmark) with provenance ──────────────────────
CREATE TABLE IF NOT EXISTS model_benchmarks (
  provider    TEXT NOT NULL,
  model_id    TEXT NOT NULL,
  benchmark   TEXT NOT NULL,
  score       REAL,
  source      TEXT NOT NULL,
  fetched_at  TEXT NOT NULL,
  PRIMARY KEY (provider, model_id, benchmark)
);

-- ── curated_routing: versioned, append-only ───────────────────────────────────
-- Version is the primary identity: replacing bumps it and never rewrites a
-- prior version. The refresh path only ever SELECTs.
CREATE TABLE IF NOT EXISTS curated_routing (
  version     INTEGER NOT NULL PRIMARY KEY,
  rules       TEXT    NOT NULL DEFAULT '[]',
  note        TEXT,
  created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── model_registry_health: last health snapshot ───────────────────────────────
CREATE TABLE IF NOT EXISTS model_registry_health (
  backend          TEXT PRIMARY KEY,
  model_count      INTEGER NOT NULL DEFAULT 0,
  priced_count     INTEGER NOT NULL DEFAULT 0,
  unpriced_count   INTEGER NOT NULL DEFAULT 0,
  curated_version  INTEGER NOT NULL DEFAULT 0,
  last_sync_at     TEXT,
  recorded_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── model_registry_sync_log: additive ABC columns ────────────────────────────
-- The table already exists (migrations.sql) with models_seen/models_upserted/
-- status. The ABC names the counters models_found/models_added/
-- models_updated/models_deprecated plus duration_ms; add them nullable so the
-- legacy inserts keep working unchanged.
ALTER TABLE model_registry_sync_log ADD COLUMN models_found INTEGER;
ALTER TABLE model_registry_sync_log ADD COLUMN models_added INTEGER;
ALTER TABLE model_registry_sync_log ADD COLUMN models_updated INTEGER;
ALTER TABLE model_registry_sync_log ADD COLUMN models_deprecated INTEGER;
ALTER TABLE model_registry_sync_log ADD COLUMN duration_ms INTEGER;
