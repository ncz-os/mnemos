-- =============================================================================
-- MNEMOS v1 Multi-User Migration
-- Fully additive — no DROP or RENAME of existing columns
-- Idempotent: safe to run on a live database with existing data
-- Run as superuser: sudo -u postgres psql -d mnemos -f migrations_v1_multiuser.sql
-- PostgreSQL 14+ required
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- 1. New tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    display_name TEXT,
    email        TEXT UNIQUE,
    role         TEXT NOT NULL DEFAULT 'user'
                 CHECK (role IN ('user', 'root')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS groups (
    id           TEXT PRIMARY KEY,
    display_name TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_groups (
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);

CREATE TABLE IF NOT EXISTS api_keys (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash   TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    label      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used  TIMESTAMPTZ,
    revoked    BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_active  ON api_keys(key_hash) WHERE NOT revoked;

-- ---------------------------------------------------------------------------
-- 2. Seed default user for personal-profile installs (idempotent)
-- ---------------------------------------------------------------------------
INSERT INTO users (id, display_name, role)
VALUES ('default', 'Default User', 'root')
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. Additive columns on memories
-- ---------------------------------------------------------------------------
ALTER TABLE memories ADD COLUMN IF NOT EXISTS owner_id         TEXT DEFAULT 'default';
ALTER TABLE memories ADD COLUMN IF NOT EXISTS group_id         TEXT DEFAULT NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS namespace        TEXT DEFAULT 'default';
ALTER TABLE memories ADD COLUMN IF NOT EXISTS permission_mode  SMALLINT DEFAULT 600;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_model     TEXT DEFAULT NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_provider  TEXT DEFAULT NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_session   TEXT DEFAULT NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_agent     TEXT DEFAULT NULL;

-- ---------------------------------------------------------------------------
-- 4. Backfill existing rows
-- ---------------------------------------------------------------------------
UPDATE memories
SET owner_id        = 'default',
    namespace       = 'default',
    permission_mode = 600
WHERE owner_id IS NULL;

-- ---------------------------------------------------------------------------
-- 5. Set NOT NULL now that backfill is complete
-- ---------------------------------------------------------------------------
ALTER TABLE memories ALTER COLUMN owner_id        SET NOT NULL;
ALTER TABLE memories ALTER COLUMN namespace       SET NOT NULL;
ALTER TABLE memories ALTER COLUMN permission_mode SET NOT NULL;

-- ---------------------------------------------------------------------------
-- 6. Indexes on memories
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_memories_owner_id  ON memories(owner_id);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace);
CREATE INDEX IF NOT EXISTS idx_memories_group_id  ON memories(group_id) WHERE group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_owner_cat ON memories(owner_id, category);

-- ---------------------------------------------------------------------------
-- 7. Row Level Security
--
-- Activation: RLS is defined here but ENABLED separately.
-- Personal profile: call nothing — RLS stays off, all rows visible.
-- Team/Enterprise: install.py runs:
--   ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE memories FORCE ROW LEVEL SECURITY;
--
-- Policy design:
--   personal_bypass — when no user context set (mnemos.current_user_id IS NULL),
--                     allow all operations. This is the personal-profile path.
--   owner_*         — owner can read/write their own memories.
--   group_select    — group members can read memories with permission_mode >= 640.
--   world_select    — anyone can read memories with ones-digit >= 4 (644, 664, etc).
-- ---------------------------------------------------------------------------

DO $$ BEGIN
    CREATE POLICY mnemos_personal_bypass ON memories
        AS PERMISSIVE FOR ALL
        USING      (current_setting('mnemos.current_user_id', TRUE) IS NULL
                    OR current_setting('mnemos.current_user_id', TRUE) = '')
        WITH CHECK (current_setting('mnemos.current_user_id', TRUE) IS NULL
                    OR current_setting('mnemos.current_user_id', TRUE) = '');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY mnemos_owner_select ON memories
        FOR SELECT
        USING (
            current_setting('mnemos.current_user_id', TRUE) = owner_id
            OR current_setting('mnemos.current_role', TRUE) = 'root'
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY mnemos_owner_insert ON memories
        FOR INSERT
        WITH CHECK (
            current_setting('mnemos.current_user_id', TRUE) = owner_id
            OR current_setting('mnemos.current_role', TRUE) = 'root'
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY mnemos_owner_update ON memories
        FOR UPDATE
        USING (
            current_setting('mnemos.current_user_id', TRUE) = owner_id
            OR current_setting('mnemos.current_role', TRUE) = 'root'
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY mnemos_owner_delete ON memories
        FOR DELETE
        USING (
            current_setting('mnemos.current_user_id', TRUE) = owner_id
            OR current_setting('mnemos.current_role', TRUE) = 'root'
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY mnemos_group_select ON memories
        FOR SELECT
        USING (
            permission_mode >= 640
            AND group_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM user_groups ug
                WHERE ug.group_id = memories.group_id
                  AND ug.user_id  = current_setting('mnemos.current_user_id', TRUE)
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- World-readable: ones digit of octal >= 4 (handles 644, 664, 774, etc.)
DO $$ BEGIN
    CREATE POLICY mnemos_world_select ON memories
        FOR SELECT
        USING ((permission_mode % 10) >= 4);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- 8. Grants
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON users       TO mnemos_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON groups      TO mnemos_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_groups TO mnemos_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON api_keys    TO mnemos_user;

COMMIT;
