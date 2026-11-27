-- 0073: ModelRegistryRepository phase 2 — Oracle physical shape.
-- Mirror of migrations/0073_model_registry_repository_phase2.sql (canonical PG).
-- See that file and the SQLite mirror
-- (migrations_sqlite/migrations_v7_1_model_registry_repository_sqlite.sql) for
-- the nullable-price rule: NULL represents an unknown price and 0 represents a
-- free model.
--
-- Idempotence contract (same as 0044 / 0071): Oracle replay is benign because
-- schema.py treats ORA-01430 (column already exists) as a no-op for ALTER
-- statements; CREATE TABLE / CREATE INDEX are guarded via USER_TABLES /
-- USER_INDEXES counts.
--
-- Translation choices that cannot be verified without a live Oracle instance:
--   * The correlated-subquery form inside the MERGE USING block below (a
--     WHERE-clause anti-join against the target table). This is legal in
--     Oracle 23ai/26ai, but it is not exercised against a live DB here.
--   * ALTER COLUMN ... DROP DEFAULT requires the column to currently have a
--     default. On this provisioning path the price columns carry no default
--     (they were added NULL-able below, and 0044-style live shapes were
--     reconciled by the DROP), so the guard swallows ORA-01430-style failures
--     silently; if a cluster carries a pre-0073 shape with defaults the DROP
--     applies.
--   * GRANT statements are omitted: this Oracle suite provisions schema with
--     the deploy user, matching every other migration in this directory.

-- ── model_registry: ABC columns missing from the Oracle physical shape ────────
-- price_* are nullable on purpose (NULL = unpriced). pricing_model / band have
-- defaults so every legacy row round-trips with the ABC's defaults.

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (max_output_tokens NUMBER(12))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (price_in NUMBER(12,6))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (price_out NUMBER(12,6))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (price_cached NUMBER(12,6))';
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

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (pricing_model VARCHAR2(50) DEFAULT ''unknown'' NOT NULL)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (band VARCHAR2(50) DEFAULT ''standard'' NOT NULL)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry ADD (alias_of VARCHAR2(400))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

-- Migration 0044 supplied zero defaults on live clusters. Only timestamped
-- legacy prices represent an observation, including explicit zero prices; the
-- DROP below removes any residual zero-defaults so NULL means "unpriced".
-- The guard keeps replay benign on shapes that carry no default.
BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry MODIFY (price_in DROP DEFAULT)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE IN (-1430, -1424, -00904, -01437) THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry MODIFY (price_out DROP DEFAULT)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE IN (-1430, -1424, -00904, -01437) THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry MODIFY (price_cached DROP DEFAULT)';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE IN (-1430, -1424, -00904, -01437) THEN NULL; ELSE RAISE; END IF;
END;
/

UPDATE model_registry
SET price_in = NULL, price_out = NULL, price_cached = NULL
WHERE price_updated_at IS NULL
  AND (price_in IS NOT NULL OR price_out IS NOT NULL OR price_cached IS NOT NULL);

-- The (provider, model_id) uniqueness makes alias IDs unique per provider.
-- Multiple distinct aliases can point to the same target. On replay the index
-- is already gone: ORA-01418 (index does not exist) is the benign path.
BEGIN
    EXECUTE IMMEDIATE 'DROP INDEX uq_model_registry_provider_alias';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -01418 THEN NULL; ELSE RAISE; END IF;
END;
/

-- ── model_prices: (model, pricing_model, band) rows ──────────────────────────
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'MODEL_PRICES';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE model_prices (
        provider      VARCHAR2(50)  NOT NULL,
        model_id      VARCHAR2(400) NOT NULL,
        pricing_model VARCHAR2(50)  NOT NULL DEFAULT 'unknown'
                      CHECK (pricing_model IN ('payg', 'subscription', 'free_tier', 'local', 'unknown')),
        band          VARCHAR2(50)  NOT NULL DEFAULT 'standard'
                      CHECK (band IN ('standard', 'peak', 'off_peak')),
        price_in      NUMBER(12,6),
        price_out     NUMBER(12,6),
        price_cached  NUMBER(12,6),
        observed_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT SYSTIMESTAMP,
        CONSTRAINT pk_model_prices PRIMARY KEY (provider, model_id, pricing_model, band)
      )]';
  END IF;
END;
/

DECLARE
  v_n NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_n FROM user_indexes WHERE index_name = 'IDX_MODEL_PRICES_MODEL';
  IF v_n = 0 THEN
    EXECUTE IMMEDIATE 'CREATE INDEX idx_model_prices_model ON model_prices (provider, model_id)';
  END IF;
END;
/

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
) src
ON (p.provider = src.provider
    AND p.model_id = src.model_id
    AND p.pricing_model = src.pricing_model
    AND p.band = src.band)
WHEN NOT MATCHED THEN
    INSERT (provider, model_id, pricing_model, band, price_in, price_out, price_cached, observed_at)
    VALUES (src.provider, src.model_id, src.pricing_model, src.band,
            src.price_in, src.price_out, src.price_cached, src.observed_at);

-- ── model_registry_field_obs: per-field value + provenance ───────────────────
-- One row per (provider, model_id, field); source/fetched_at belong to the
-- FIELD. ``record_field_obs`` / ``merge_fields`` already query this table; no
-- Oracle migration created it, so fresh clusters would fail the write.
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'MODEL_REGISTRY_FIELD_OBS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE model_registry_field_obs (
        provider    VARCHAR2(50)  NOT NULL,
        model_id    VARCHAR2(400) NOT NULL,
        field       VARCHAR2(400) NOT NULL,
        value       VARCHAR2(4000),
        source      VARCHAR2(400) NOT NULL,
        fetched_at  TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT pk_model_registry_field_obs PRIMARY KEY (provider, model_id, field)
      )]';
  END IF;
END;
/

DECLARE
  v_n NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_n FROM user_indexes WHERE index_name = 'IDX_MODEL_REGISTRY_FIELD_OBS_SOURCE';
  IF v_n = 0 THEN
    EXECUTE IMMEDIATE 'CREATE INDEX idx_model_registry_field_obs_source ON model_registry_field_obs (source)';
  END IF;
END;
/

-- ── model_benchmarks: (model, benchmark) with provenance ────────────────────
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'MODEL_BENCHMARKS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE model_benchmarks (
        provider    VARCHAR2(50)  NOT NULL,
        model_id    VARCHAR2(400) NOT NULL,
        benchmark   VARCHAR2(400) NOT NULL,
        score       NUMBER(12,6),
        source      VARCHAR2(400) NOT NULL,
        fetched_at  TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT pk_model_benchmarks PRIMARY KEY (provider, model_id, benchmark)
      )]';
  END IF;
END;
/

-- One-time backfill: the legacy arena score lives on the ``model_registry``
-- row (column present since 0037). The correlated ``NOT EXISTS`` inside the
-- MERGE USING keeps it per-model (replay-safe and partial-run-safe) so
-- ``export_snapshot`` does not lose the score for clusters populated before
-- this migration.
MERGE INTO model_benchmarks b
USING (
    SELECT m.provider, m.model_id, 'arena_score' benchmark, m.arena_score score,
           'model_registry_row' source, SYSTIMESTAMP fetched_at
    FROM model_registry m
    WHERE m.alias_of IS NULL
      AND m.arena_score IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM model_benchmarks b
          WHERE b.provider = m.provider AND b.model_id = m.model_id
      )
) src
ON (b.provider = src.provider AND b.model_id = src.model_id AND b.benchmark = src.benchmark)
WHEN NOT MATCHED THEN
    INSERT (provider, model_id, benchmark, score, source, fetched_at)
    VALUES (src.provider, src.model_id, src.benchmark, src.score, src.source, src.fetched_at);

-- ── curated_routing: versioned, append-only ──────────────────────────────────
-- Version is the primary identity: ``curated_routing_replace`` bumps it and
-- never rewrites a prior version. JSONB becomes CLOB CHECK (IS JSON) per the
-- Oracle JSON convention (0001, 0002, 0003).
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'CURATED_ROUTING';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE curated_routing (
        version     NUMBER(19) NOT NULL PRIMARY KEY,
        rules       CLOB       NOT NULL DEFAULT '[]' CHECK (rules IS JSON),
        note        VARCHAR2(4000),
        created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT SYSTIMESTAMP
      )]';
  END IF;
END;
/

-- ── model_registry_health: last health snapshot per backend ──────────────────
-- ``curated_version`` is NUMBER(19) to match ``curated_routing.version``.
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'MODEL_REGISTRY_HEALTH';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE model_registry_health (
        backend         VARCHAR2(50) PRIMARY KEY,
        model_count     NUMBER(12)  NOT NULL DEFAULT 0,
        priced_count    NUMBER(12)  NOT NULL DEFAULT 0,
        unpriced_count  NUMBER(12)  NOT NULL DEFAULT 0,
        curated_version NUMBER(19)  NOT NULL DEFAULT 0,
        last_sync_at    TIMESTAMP WITH TIME ZONE,
        recorded_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT SYSTIMESTAMP
      )]';
  END IF;
END;
/

-- Replay guard: if an earlier shape provisioned a narrower type, widen to
-- NUMBER(19) so it matches curated_routing.version.
BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry_health MODIFY (curated_version NUMBER(19))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE IN (-1430, -00904) THEN NULL; ELSE RAISE; END IF;
END;
/

-- ── model_registry_sync_log: additive ABC counters (0049 parity) ────────────
-- The table exists with ``models_found`` ... ``duration_ms`` from 0049 (and
-- 0037); the guards keep replay safe on clusters that only have the legacy
-- shape.
BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD (models_found NUMBER(12))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD (models_added NUMBER(12))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD (models_updated NUMBER(12))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD (models_deprecated NUMBER(12))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE model_registry_sync_log ADD (duration_ms NUMBER(12))';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE = -1430 THEN NULL; ELSE RAISE; END IF;
END;
/

-- ── legacy curated_routing_versions guard ────────────────────────────────────
-- If an older cluster ever carried live curated versions in a separate
-- ``curated_routing_versions`` table, copy them into the authoritative
-- ``curated_routing`` table (skipping versions that already exist) so
-- ``health_read`` / ``curated_routing_get`` do not silently report version 0
-- after the migration. No-op when the old table does not exist.
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'CURATED_ROUTING_VERSIONS';
  IF v_count > 0 THEN
    EXECUTE IMMEDIATE q'[
      MERGE INTO curated_routing dst
      USING (SELECT version, rules, note, created_at FROM curated_routing_versions) src
      ON (dst.version = src.version)
      WHEN NOT MATCHED THEN
        INSERT (version, rules, note, created_at)
        VALUES (src.version, src.rules, src.note, src.created_at)]';
  END IF;
END;
/

COMMENT ON TABLE model_prices IS
    'Band-aware price rows for ModelRegistryRepository (provider, model_id, pricing_model, band).';
COMMENT ON TABLE model_benchmarks IS
    'Benchmark scores with per-row provenance for ModelRegistryRepository.';
COMMENT ON TABLE curated_routing IS
    'Versioned, append-only curated routing layer owned by operators.';
COMMENT ON TABLE model_registry_health IS
    'Last health snapshot written by ModelRegistryRepository.health_write.';
