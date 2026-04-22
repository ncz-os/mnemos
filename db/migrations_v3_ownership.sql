-- =============================================================================
-- v3.0.x owner_id backfill migration — multi-tenant safety
-- =============================================================================
-- Adds owner_id to tables that previously had no per-user scoping:
--   * state                — global KV store → per-owner KV
--   * journal              — global journal → per-owner journal
--   * entities             — global entity registry → per-owner (or shared) registry
--   * graeae_consultations — global consult log → per-owner consult log
-- Existing rows are backfilled with owner_id = 'default' which matches the
-- `personal_user_id` fallback in api/auth.py. Handlers enforce a WHERE
-- owner_id = $current_user clause on all reads/writes.
-- Idempotent — safe to re-run.
-- =============================================================================

-- state -----------------------------------------------------------------------
ALTER TABLE state ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT 'default';
-- PK was (key). Switch to composite (owner_id, key). Guarded so re-runs don't
-- fail on missing constraint name.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'state_pkey' AND contype = 'p'
    ) THEN
        -- If the current PK is still just (key), drop and replace with composite.
        IF (
            SELECT array_agg(attname ORDER BY attnum)::text
            FROM pg_attribute a
            JOIN pg_index i ON i.indexrelid = a.attrelid OR i.indrelid = a.attrelid
            WHERE a.attrelid = 'state'::regclass
              AND a.attnum = ANY(i.indkey)
              AND i.indisprimary
        ) = '{key}' THEN
            ALTER TABLE state DROP CONSTRAINT state_pkey;
            ALTER TABLE state ADD PRIMARY KEY (owner_id, key);
        END IF;
    END IF;
END$$;
CREATE INDEX IF NOT EXISTS idx_state_owner ON state(owner_id);

-- journal ---------------------------------------------------------------------
ALTER TABLE journal ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_journal_owner       ON journal(owner_id);
CREATE INDEX IF NOT EXISTS idx_journal_owner_date  ON journal(owner_id, entry_date DESC);

-- entities --------------------------------------------------------------------
ALTER TABLE entities ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_entities_owner ON entities(owner_id);
-- Existing unique constraint was (entity_type, name) cross-owner. Now each
-- owner has their own namespace; replace with (owner_id, entity_type, name).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'entities_entity_type_name_key'
          AND conrelid = 'entities'::regclass
    ) THEN
        ALTER TABLE entities DROP CONSTRAINT entities_entity_type_name_key;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'entities_owner_type_name_key'
          AND conrelid = 'entities'::regclass
    ) THEN
        ALTER TABLE entities ADD CONSTRAINT entities_owner_type_name_key
            UNIQUE (owner_id, entity_type, name);
    END IF;
END$$;

-- graeae_consultations --------------------------------------------------------
ALTER TABLE graeae_consultations ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_graeae_consultations_owner ON graeae_consultations(owner_id);

-- federation_peers ------------------------------------------------------------
-- Document the plaintext-token caveat directly in the schema. The column is
-- readable by anyone with database access; encrypt at rest via a
-- KMS-backed envelope key or pgcrypto if that matters for your threat model.
COMMENT ON COLUMN federation_peers.auth_token IS
    'Bearer token sent to remote peers. Stored in plaintext — protect with '
    'filesystem-level encryption or a wrapper view if your threat model '
    'requires at-rest encryption.';
