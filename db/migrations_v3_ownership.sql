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
-- Identify the old constraint by column set rather than by name, because the
-- auto-generated name can vary across PG versions and manual schema tweaks.
DO $$
DECLARE
    _entity_type_attnum SMALLINT;
    _name_attnum        SMALLINT;
    _owner_attnum       SMALLINT;
    _old_conname        TEXT;
BEGIN
    SELECT attnum INTO _entity_type_attnum FROM pg_attribute
     WHERE attrelid = 'entities'::regclass AND attname = 'entity_type';
    SELECT attnum INTO _name_attnum FROM pg_attribute
     WHERE attrelid = 'entities'::regclass AND attname = 'name';
    SELECT attnum INTO _owner_attnum FROM pg_attribute
     WHERE attrelid = 'entities'::regclass AND attname = 'owner_id';

    -- Drop any UNIQUE constraint whose column set is exactly (entity_type, name).
    FOR _old_conname IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'entities'::regclass
           AND contype  = 'u'
           AND conkey   = ARRAY[_entity_type_attnum, _name_attnum]::SMALLINT[]
    LOOP
        EXECUTE format('ALTER TABLE entities DROP CONSTRAINT %I', _old_conname);
    END LOOP;

    -- Create the owner-scoped UNIQUE only if no constraint with the right
    -- column set already exists.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'entities'::regclass
           AND contype  = 'u'
           AND conkey   = ARRAY[_owner_attnum, _entity_type_attnum, _name_attnum]::SMALLINT[]
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

-- memories.permission_mode convention -----------------------------------------
-- Decimal digits that read like octal Unix mode bits:
--   600 = owner rw, no group/others         (default for local memories)
--   644 = owner rw, group r, others r       (federated / publicly-readable)
--   660 = owner rw, group rw                (group-editable)
-- The federation feed uses `(permission_mode % 10) >= 4` so the "others"
-- digit having the read bit opts a memory in. Do NOT store true octal
-- values (PG SMALLINT 420 = 0o644) without updating the filter first.
COMMENT ON COLUMN memories.permission_mode IS
    'Decimal representation of Unix-style mode (600=owner-only, 644=federated-readable). '
    'Federation feed filter: (permission_mode % 10) >= 4 means others-readable.';
