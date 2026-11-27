--#SET TERMINATOR @
-- 0073: ModelRegistryRepository phase 2 — IBM Db2 12.1 native dialect.
-- Mirrors db/migrations/0073_model_registry_repository_phase2.sql (canonical PG).
-- See that file and the SQLite mirror
-- (migrations_sqlite/migrations_v7_1_model_registry_repository_sqlite.sql) for
-- the nullable-price rule: NULL represents an unknown price and 0 represents a
-- free model.
--
-- Idempotence contract (same as 0044): Db2 has no PG-style IF NOT EXISTS and
-- no Oracle-style `/` terminator (the runner splits on `@`). ADD COLUMN is
-- guarded via a continue handler for the column-exists SQLSTATE; CREATE TABLE
-- / CREATE INDEX via the object-exists SQLSTATE.
--
-- Translation choices that cannot be verified without a live Db2 instance:
--   * ALTER COLUMN ... DROP DEFAULT inside the 42703 / 428D-guarded blocks:
--     legal in Db2 12.1, but the guard swallows every failure in those
--     families, so on a shape that never carried a default the DROP is a
--     silent no-op rather than a verified apply.
--   * The correlated ``NOT EXISTS`` anti-join inside the MERGE USING block:
--     legal native Db2 syntax, not exercised against a live DB here. The legacy
--     curated_routing_versions copy is additionally gated on SYSCAT.TABLES
--     (catalog check) because a static MERGE FROM that table is validated at
--     compile time and a continue handler cannot swallow SQL0204N.
--   * JSON validation uses the established 0044 convention
--     (``CHECK (rules IS NULL OR SYSTOOLS.JSON2BSON(rules) IS NOT NULL)``);
--     on Db2 12.1.x native the ``SYSTOOLS`` schema reference resolves inside
--     the check constraint. The native ``JSON`` data type is not used, matching
--     every other migration in this directory.

-- ── model_registry: ABC columns missing from the Db2 physical shape ──────────
-- price_* are nullable on purpose (NULL = unpriced). pricing_model / band have
-- defaults so every legacy row round-trips with the ABC's defaults.

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN max_output_tokens INTEGER';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_in DECIMAL(12,6)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_out DECIMAL(12,6)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_cached DECIMAL(12,6)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN price_updated_at TIMESTAMP(6)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN pricing_model VARCHAR(50) NOT NULL WITH DEFAULT ''unknown''';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN band VARCHAR(50) NOT NULL WITH DEFAULT ''standard''';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD COLUMN alias_of VARCHAR(400)';
END@

-- Migration 0044 supplied zero defaults on live shapes. Only timestamped
-- legacy prices represent an observation, including explicit zero prices; the
-- DROP removes any residual zero-defaults so NULL means "unpriced". The guard
-- keeps replay benign on shapes that carry no default: 42703 is the
-- undefined-column family and 428D is the column-state family for a DROP
-- DEFAULT on a column with no default.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42703' BEGIN END;
  DECLARE CONTINUE HANDLER FOR SQLSTATE '428D' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ALTER COLUMN price_in DROP DEFAULT';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42703' BEGIN END;
  DECLARE CONTINUE HANDLER FOR SQLSTATE '428D' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ALTER COLUMN price_out DROP DEFAULT';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42703' BEGIN END;
  DECLARE CONTINUE HANDLER FOR SQLSTATE '428D' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry ALTER COLUMN price_cached DROP DEFAULT';
END@

UPDATE model_registry
SET price_in = NULL, price_out = NULL, price_cached = NULL
WHERE price_updated_at IS NULL
  AND (price_in IS NOT NULL OR price_out IS NOT NULL OR price_cached IS NOT NULL)@

-- The (provider, model_id) uniqueness makes alias IDs unique per provider.
-- Multiple distinct aliases can point to the same target. On replay the index
-- is already gone: 42704 (object not found) is the missing-index state, 42601
-- is the SQLCODE -204 family; both are benign.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42704' BEGIN END;
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42601' BEGIN END;
  EXECUTE IMMEDIATE 'DROP INDEX uq_model_registry_provider_alias';
END@

-- ── model_prices: (model, pricing_model, band) rows ──────────────────────────
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE model_prices (
      provider      VARCHAR(50)  NOT NULL,
      model_id      VARCHAR(400) NOT NULL,
      pricing_model VARCHAR(50)  NOT NULL WITH DEFAULT ''unknown''
        CHECK (pricing_model IN (''payg'', ''subscription'', ''free_tier'', ''local'', ''unknown'')),
      band          VARCHAR(50)  NOT NULL WITH DEFAULT ''standard''
        CHECK (band IN (''standard'', ''peak'', ''off_peak'')),
      price_in      DECIMAL(12,6),
      price_out     DECIMAL(12,6),
      price_cached  DECIMAL(12,6),
      observed_at   TIMESTAMP(6) NOT NULL WITH DEFAULT CURRENT TIMESTAMP,
      CONSTRAINT pk_model_prices PRIMARY KEY (provider, model_id, pricing_model, band)
    )';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_model_prices_model ON model_prices (provider, model_id)';
END@

-- Copy timestamped legacy prices for canonical models without band rows.
-- Preserve the observation timestamp and each price, including NULL and zero.
MERGE INTO model_prices p
USING (
    SELECT m.provider, m.model_id, 'unknown' pricing_model, 'standard' band,
           m.price_in, m.price_out, m.price_cached, m.price_updated_at observed_at
    FROM model_registry m
    WHERE m.alias_of IS NULL
      AND m.price_updated_at IS NOT NULL
      AND (m.price_in IS NOT NULL OR m.price_out IS NOT NULL OR m.price_cached IS NOT NULL)
      AND NOT EXISTS (
          SELECT 1 FROM model_prices p
          WHERE p.provider = m.provider AND p.model_id = m.model_id
      )
) AS src(provider, model_id, pricing_model, band, price_in, price_out, price_cached, observed_at)
ON p.provider = src.provider
   AND p.model_id = src.model_id
   AND p.pricing_model = src.pricing_model
   AND p.band = src.band
WHEN NOT MATCHED THEN
    INSERT (provider, model_id, pricing_model, band, price_in, price_out, price_cached, observed_at)
    VALUES (src.provider, src.model_id, src.pricing_model, src.band,
            src.price_in, src.price_out, src.price_cached, src.observed_at)@

-- ── model_registry_field_obs: per-field value + provenance ───────────────────
-- One row per (provider, model_id, field); source/fetched_at belong to the
-- FIELD. ``record_field_obs`` / ``merge_fields`` already query this table; no
-- Db2 migration created it, so fresh clusters would fail the write.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE model_registry_field_obs (
      provider    VARCHAR(50)  NOT NULL,
      model_id    VARCHAR(400) NOT NULL,
      field       VARCHAR(400) NOT NULL,
      value       VARCHAR(4000),
      source      VARCHAR(400) NOT NULL,
      fetched_at  TIMESTAMP(6) NOT NULL,
      CONSTRAINT pk_model_registry_field_obs PRIMARY KEY (provider, model_id, field)
    )';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_model_registry_field_obs_source ON model_registry_field_obs (source)';
END@

-- ── model_benchmarks: (model, benchmark) with provenance ────────────────────
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE model_benchmarks (
      provider    VARCHAR(50)  NOT NULL,
      model_id    VARCHAR(400) NOT NULL,
      benchmark   VARCHAR(400) NOT NULL,
      score       DECIMAL(12,6),
      source      VARCHAR(400) NOT NULL,
      fetched_at  TIMESTAMP(6) NOT NULL,
      CONSTRAINT pk_model_benchmarks PRIMARY KEY (provider, model_id, benchmark)
    )';
END@

-- One-time backfill: the legacy arena score lives on the ``model_registry``
-- row (column present since 0037). The correlated ``NOT EXISTS`` inside the
-- MERGE USING keeps it per-model (replay-safe and partial-run-safe) so
-- ``export_snapshot`` does not lose the score for clusters populated before
-- this migration.
MERGE INTO model_benchmarks b
USING (
    SELECT m.provider, m.model_id, 'arena_score' benchmark, m.arena_score score,
           'model_registry_row' source, CURRENT TIMESTAMP fetched_at
    FROM model_registry m
    WHERE m.alias_of IS NULL
      AND m.arena_score IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM model_benchmarks b
          WHERE b.provider = m.provider AND b.model_id = m.model_id
      )
) AS src(provider, model_id, benchmark, score, source, fetched_at)
ON b.provider = src.provider
   AND b.model_id = src.model_id
   AND b.benchmark = src.benchmark
WHEN NOT MATCHED THEN
    INSERT (provider, model_id, benchmark, score, source, fetched_at)
    VALUES (src.provider, src.model_id, src.benchmark, src.score, src.source, src.fetched_at)@

-- ── curated_routing: versioned, append-only ──────────────────────────────────
-- Version is the primary identity: ``curated_routing_replace`` bumps it and
-- never rewrites a prior version. JSONB becomes CLOB with the 0044 JSON check
-- convention.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE curated_routing (
      version    BIGINT NOT NULL PRIMARY KEY,
      rules      CLOB(1M) INLINE LENGTH 1024 NOT NULL WITH DEFAULT ''[]''
        CHECK (SYSTOOLS.JSON2BSON(rules) IS NOT NULL),
      note       VARCHAR(4000),
      created_at TIMESTAMP(6) NOT NULL WITH DEFAULT CURRENT TIMESTAMP
    )';
END@

-- ── model_registry_health: last health snapshot per backend ──────────────────
-- ``curated_version`` is BIGINT to match ``curated_routing.version``.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE model_registry_health (
      backend         VARCHAR(50) PRIMARY KEY,
      model_count     INTEGER NOT NULL WITH DEFAULT 0,
      priced_count    INTEGER NOT NULL WITH DEFAULT 0,
      unpriced_count  INTEGER NOT NULL WITH DEFAULT 0,
      curated_version BIGINT NOT NULL WITH DEFAULT 0,
      last_sync_at    TIMESTAMP(6),
      recorded_at     TIMESTAMP(6) NOT NULL WITH DEFAULT CURRENT TIMESTAMP
    )';
END@

-- Replay guard: if an earlier shape provisioned a narrower type, widen to
-- BIGINT so it matches curated_routing.version.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry_health ALTER COLUMN curated_version SET DATA TYPE BIGINT';
END@

-- ── model_registry_sync_log: additive ABC counters (0049 parity) ────────────
-- The table exists with ``models_found`` ... ``duration_ms`` from 0049; the
-- guards keep replay safe on clusters that only have the legacy shape.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD COLUMN models_found INTEGER';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD COLUMN models_added INTEGER';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD COLUMN models_updated INTEGER';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD COLUMN models_deprecated INTEGER';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
  EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD COLUMN duration_ms INTEGER';
END@

-- ── legacy curated_routing_versions guard ────────────────────────────────────
-- If an older cluster ever carried live curated versions in a separate
-- ``curated_routing_versions`` table, copy them into the authoritative
-- ``curated_routing`` table (skipping versions that already exist) so
-- ``health_read`` / ``curated_routing_get`` do not silently report version 0
-- after the migration.
--
-- The MERGE is emitted only when the catalog (SYSCAT.TABLES) confirms the
-- legacy table exists. A static ``FROM curated_routing_versions`` inside a
-- compound statement is validated at COMPILE time (SQL0204N) on clusters that
-- never carried it, so a CONTINUE HANDLER for SQLSTATE 42704 cannot swallow
-- it; the catalog gate makes the block a true no-op there.
BEGIN
  FOR c AS
    SELECT COUNT(*) AS has_tbl FROM SYSCAT.TABLES
     WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'CURATED_ROUTING_VERSIONS'
  DO
    IF c.has_tbl > 0 THEN
      MERGE INTO curated_routing dst
      USING (
          SELECT version, rules, note, created_at FROM curated_routing_versions
      ) AS src(version, rules, note, created_at)
      ON dst.version = src.version
      WHEN NOT MATCHED THEN
          INSERT (version, rules, note, created_at)
          VALUES (src.version, src.rules, src.note, src.created_at);
    END IF;
  END FOR;
END@

COMMENT ON TABLE model_prices IS 'Band-aware price rows for ModelRegistryRepository (provider, model_id, pricing_model, band)'@
COMMENT ON TABLE model_benchmarks IS 'Benchmark scores with per-row provenance for ModelRegistryRepository'@
COMMENT ON TABLE curated_routing IS 'Versioned, append-only curated routing layer owned by operators'@
COMMENT ON TABLE model_registry_health IS 'Last health snapshot written by ModelRegistryRepository.health_write'@