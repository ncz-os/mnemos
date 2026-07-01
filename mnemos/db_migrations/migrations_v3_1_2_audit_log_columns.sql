-- MNEMOS v3.1.2 — graeae_audit_log column backfill
--
-- Databases that applied migrations_v2_versioning.sql first got the
-- v2 shape of graeae_audit_log, which is missing the prompt /
-- response_text / prev_chain_hash / model / latency_ms / cost_usd
-- columns introduced in migrations_v3_graeae_unified.sql. Because
-- that v3 migration used `CREATE TABLE IF NOT EXISTS`, it was a
-- no-op on those databases and the columns never landed.
--
-- The consultations handler (api/handlers/consultations.py) writes
-- to prompt / response_text / prev_chain_hash via an INSERT, so
-- missing columns surface as:
--     asyncpg.exceptions.UndefinedColumnError:
--         column "prompt" of relation "graeae_audit_log" does not exist
-- and /v1/consultations returns 503 with
--     "Consultation persistence failed; audit trail is required."
--
-- This migration adds the missing columns idempotently. All new
-- columns are nullable so existing audit rows (which predate the
-- plaintext-retention policy) aren't affected.

ALTER TABLE graeae_audit_log ADD COLUMN IF NOT EXISTS prompt          TEXT;
ALTER TABLE graeae_audit_log ADD COLUMN IF NOT EXISTS response_text   TEXT;
ALTER TABLE graeae_audit_log ADD COLUMN IF NOT EXISTS prev_chain_hash VARCHAR(64);
ALTER TABLE graeae_audit_log ADD COLUMN IF NOT EXISTS model           VARCHAR(100);
ALTER TABLE graeae_audit_log ADD COLUMN IF NOT EXISTS latency_ms      INT;
ALTER TABLE graeae_audit_log ADD COLUMN IF NOT EXISTS cost_usd        FLOAT;
