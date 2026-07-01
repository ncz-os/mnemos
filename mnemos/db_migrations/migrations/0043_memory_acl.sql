-- =============================================================================
-- MNEMOS v6.3 — per-principal ACL escape hatch + delegated group-admin
-- Fully additive — no DROP or RENAME of existing columns
-- Idempotent: safe to re-run on a live database
-- Run as superuser: sudo -u postgres psql -d mnemos -f migrations_v6_3_memory_acl.sql
-- PostgreSQL 14+ required
--
-- Why this exists
-- ---------------
-- The v1 model gives each memory exactly one owner, one group_id, and Unix
-- mode bits. That cannot express "share THIS memory with a second group" or
-- "share it with one named user" without relaxing world/group bits on the
-- whole row. memory_acl is the per-principal escape hatch: additional grants
-- layered ON TOP of the mode bits, never instead of them. A grant only ever
-- widens visibility; it never revokes what the mode bits already allow.
--
-- user_groups.is_admin is the delegated group-admin tier: a non-root user who
-- may manage membership of the group(s) they administer, so superuser is not
-- the only principal that can add/remove members.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Per-principal ACL table
--
-- principal is a typed string: 'user:<user_id>' or 'group:<group_id>'.
-- perm is a Unix-style permission bitmask reusing the rwx convention:
--   read = 4, write = 2  (execute/1 is unused for memories).
-- A row grants `principal` the bits in `perm` on `memory_id`, regardless of
-- the memory's own owner/group/world bits.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_acl (
    memory_id  TEXT     NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    principal  TEXT     NOT NULL
               CHECK (principal LIKE 'user:%' OR principal LIKE 'group:%'),
    perm       SMALLINT NOT NULL DEFAULT 4
               CHECK (perm >= 0 AND perm <= 7),
    granted_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (memory_id, principal)
);

-- Reverse lookup: "which memories can principal X see via ACL" drives the
-- EXISTS disjunct in the read-visibility predicate.
CREATE INDEX IF NOT EXISTS idx_memory_acl_principal ON memory_acl(principal);

-- ---------------------------------------------------------------------------
-- 2. Delegated group-admin flag
-- ---------------------------------------------------------------------------
ALTER TABLE user_groups ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- ---------------------------------------------------------------------------
-- 3. RLS defense-in-depth — mnemos_acl_select
--
-- Mirrors the app-layer ACL disjunct so that even a direct SQL reader bound to
-- mnemos_user (with a principal context set) sees ACL-granted rows and nothing
-- more. RLS combines SELECT policies with OR, so this purely widens read
-- visibility to rows the caller has been granted; it cannot expose a row the
-- caller was not granted. Only the read bit (4) is honored for SELECT.
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    CREATE POLICY mnemos_acl_select ON memories
        FOR SELECT
        USING (
            EXISTS (
                SELECT 1 FROM memory_acl macl
                WHERE macl.memory_id = memories.id
                  AND (macl.perm & 4) <> 0
                  AND (
                      macl.principal = 'user:' || current_setting('mnemos.current_user_id', TRUE)
                      OR macl.principal IN (
                          SELECT 'group:' || ug.group_id
                          FROM user_groups ug
                          WHERE ug.user_id = current_setting('mnemos.current_user_id', TRUE)
                      )
                  )
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Grants (guarded — role may not exist on a bare test DB)
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    GRANT SELECT, INSERT, UPDATE, DELETE ON memory_acl TO mnemos_user;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

COMMIT;
