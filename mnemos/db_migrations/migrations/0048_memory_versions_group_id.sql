-- =============================================================================
-- MNEMOS — backfill memory_versions with group_id (GitLab #2 ncz-os/mnemos#2)
-- Fully additive — no DROP or RENAME of existing columns
-- Idempotent: safe to re-run on a live database
-- Run after migrations/0043_memory_acl.sql so the memory_acl disjunct in
--   version_visibility_predicate can compose against a properly indexed
--   snapshot table.
--
-- Why
-- ---
-- The pre-#2 version_visibility_predicate was deliberately narrower than
-- read_visibility_predicate: ``memory_versions`` carried no ``group_id``
-- column so the group-readable disjunct could not fire against historical
-- snapshots. Snapshots taken when a memory was group-readable therefore
-- fail-closed against group-only readers for /v1/memories/{id}/log,
-- /commits/{hash}, /versions, and the equivalent MCP / DAG paths.
--
-- Memory_acl is keyed on ``memory_id`` (not on snapshot id), so the
-- ACL widening does NOT require a schema change to memory_versions —
-- a single-row lookup ``EXISTS (SELECT 1 FROM memory_acl WHERE memory_id = $1)``
-- from version_visibility_predicate widens visibility to every surviving
-- snapshot of that memory atomically. Only the group branch needs the
-- column backfill on memory_versions, plus an index to keep the filter
-- non-table-scan.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. group_id backfill column
--    Pre-#2 snapshots always carry NULL group_id so the widened predicate
--    fails closed for legacy rows (legacy rows can't be group-claimed by
--    any reader). New snapshots will pick up the live memory's group_id
--    via the trigger updates in step 3 below.
-- ---------------------------------------------------------------------------
ALTER TABLE memory_versions
    ADD COLUMN IF NOT EXISTS group_id TEXT;

-- ---------------------------------------------------------------------------
-- 2. Backfill existing snapshot rows from the live memories table
--
--    For pre-existing memory_versions rows the snapshot's group_id is
--    the live memory's CURRENT group_id (we cannot reconstruct the
--    historical group_id at snapshot time without an audit log). This
--    means a snapshot of a private memory that LATER became group-readable
--    is retroactively group-readable to its history. That mirrors the
--    existing semantics of read_visibility_predicate against the live
--    row — the live-memory ACL/group read widening for issue #2 already
--    admits this trade.
--
--    Operators can override later by editing memory_versions.group_id
--    manually (the column is unaudited historic state, not a source of
--    truth). The migration is documented as such in KNOWN_LIMITATIONS.
-- ---------------------------------------------------------------------------
UPDATE memory_versions mv
SET    group_id = m.group_id
FROM   memories m
WHERE  mv.memory_id = m.id
  AND  mv.group_id IS NULL
  AND  m.group_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Trigger fn patches so the snapshot trigger writes the live memory's
--    CURRENT group_id into the new memory_versions row alongside the other
--    tenancy columns it already copies. Without this, every NEW snapshot
--    would null out group_id and the backfill above would constantly
--    re-fill on every UPDATE — wasteful and racing with concurrent writers.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mnemos_version_snapshot() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    _next_v          INTEGER;
    _by              TEXT;
    _branch          TEXT;
    _commit_hash     TEXT;
    _parent_version  UUID;
    _new_version_id  UUID;
BEGIN
    _by := NULLIF(current_setting('mnemos.current_user_id', TRUE), '');
    _branch := COALESCE(NULLIF(current_setting('mnemos.current_branch', TRUE), ''), 'main');

    IF TG_OP = 'INSERT' THEN
        _commit_hash := encode(
            sha256(convert_to(NEW.id || '|1|' || NEW.content || '|' || NOW()::text, 'UTF8')),
            'hex'
        );

        INSERT INTO memory_versions (
            memory_id, version_num, content, category, subcategory, metadata,
            verbatim_content, owner_id, namespace, permission_mode,
            source_model, source_provider, source_session, source_agent,
            snapshot_by, change_type, commit_hash, branch, parent_version_id,
            group_id
        ) VALUES (
            NEW.id, 1, NEW.content, NEW.category, NEW.subcategory, NEW.metadata,
            NEW.verbatim_content, NEW.owner_id, NEW.namespace, NEW.permission_mode,
            NEW.source_model, NEW.source_provider, NEW.source_session, NEW.source_agent,
            _by, 'create', _commit_hash, _branch, NULL,
            NEW.group_id
        ) RETURNING id INTO _new_version_id;

        INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
        VALUES (NEW.id, _branch, _new_version_id, _by)
        ON CONFLICT (memory_id, name) DO UPDATE
        SET head_version_id = EXCLUDED.head_version_id;

    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.content         IS DISTINCT FROM NEW.content
        OR OLD.category        IS DISTINCT FROM NEW.category
        OR OLD.subcategory     IS DISTINCT FROM NEW.subcategory
        OR OLD.metadata        IS DISTINCT FROM NEW.metadata
        OR OLD.verbatim_content IS DISTINCT FROM NEW.verbatim_content
        OR OLD.permission_mode IS DISTINCT FROM NEW.permission_mode
        OR OLD.namespace       IS DISTINCT FROM NEW.namespace
        OR OLD.owner_id        IS DISTINCT FROM NEW.owner_id
        OR OLD.group_id        IS DISTINCT FROM NEW.group_id
        THEN
            SELECT COALESCE(MAX(version_num), 0) + 1
            INTO   _next_v
            FROM   memory_versions
            WHERE  memory_id = NEW.id AND branch = _branch;

            SELECT head_version_id INTO _parent_version
            FROM memory_branches
            WHERE memory_id = NEW.id AND name = _branch;

            _commit_hash := encode(
                sha256(convert_to(NEW.id || '|' || _next_v::text || '|' || NEW.content || '|' || NOW()::text, 'UTF8')),
                'hex'
            );

            INSERT INTO memory_versions (
                memory_id, version_num, content, category, subcategory, metadata,
                verbatim_content, owner_id, namespace, permission_mode,
                source_model, source_provider, source_session, source_agent,
                snapshot_by, change_type, commit_hash, branch, parent_version_id,
                group_id
            ) VALUES (
                NEW.id, _next_v,
                NEW.content, NEW.category, NEW.subcategory, NEW.metadata,
                NEW.verbatim_content, NEW.owner_id, NEW.namespace, NEW.permission_mode,
                NEW.source_model, NEW.source_provider, NEW.source_session, NEW.source_agent,
                _by, 'update', _commit_hash, _branch, _parent_version,
                NEW.group_id
            ) RETURNING id INTO _new_version_id;

            UPDATE memory_branches
            SET head_version_id = _new_version_id
            WHERE memory_id = NEW.id AND name = _branch;
        END IF;

    ELSIF TG_OP = 'DELETE' THEN
        SELECT COALESCE(MAX(version_num), 0) + 1
        INTO   _next_v
        FROM   memory_versions
        WHERE  memory_id = OLD.id AND branch = _branch;

        SELECT head_version_id INTO _parent_version
        FROM memory_branches
        WHERE memory_id = OLD.id AND name = _branch;

        _commit_hash := encode(
            sha256(convert_to(OLD.id || '|' || _next_v::text || '|' || OLD.content || '|' || NOW()::text, 'UTF8')),
            'hex'
        );

        INSERT INTO memory_versions (
            memory_id, version_num, content, category, subcategory, metadata,
            verbatim_content, owner_id, namespace, permission_mode,
            source_model, source_provider, source_session, source_agent,
            snapshot_by, change_type, commit_hash, branch, parent_version_id,
            group_id
        ) VALUES (
            OLD.id, _next_v,
            OLD.content, OLD.category, OLD.subcategory, OLD.metadata,
            OLD.verbatim_content, OLD.owner_id, OLD.namespace, OLD.permission_mode,
            OLD.source_model, OLD.source_provider, OLD.source_session, OLD.source_agent,
            _by, 'delete', _commit_hash, _branch, _parent_version,
            OLD.group_id
        );
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 4. Index for the per-snapshot group filter
--    The version_visibility_predicate joins ``memory_versions.group_id
--    = ANY($groups::text[])`` and reads permission_mode alongside for
--    the Unix-bit world / group branch. A composite (memory_id, group_id)
--    index supports the post-walk filter on /v1/memories/{id}/log.
--    Partial: group_id is NULL for the original non-grouped install —
--    no point indexing those rows for a non-match.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_mv_memory_id_group_id
    ON memory_versions (memory_id, group_id)
    WHERE group_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 5. RLS-defense-in-depth (GROUP branch on the snapshot table)
--
--    Mirror the live-memory group-read widening against memory_versions
--    so a direct SQL reader bound to mnemos_user with a principal
--    context set also gets the right group-readable filter on
--    historical snapshots. RLS combines SELECT policies with OR, so
--    this purely widens read visibility to snapshots the caller is in
--    the snapshot's group for.
--
--    We deliberately do NOT add a memory_acl RLS policy on
--    memory_versions here: the application-layer EXISTS check in
--    version_visibility_predicate already covers that case (memory_acl
--    is keyed on memory_id, not on snapshot id, so it applies to every
--    snapshot of that memory atomically). RLS adds nothing the
--    application layer doesn't already enforce.
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    CREATE POLICY mnemos_version_group_select ON memory_versions
        FOR SELECT
        USING (
            (
                ((permission_mode / 10) % 10) >= 4
                AND group_id IS NOT NULL
                AND group_id = ANY(
                    SELECT 'group:' || ug.group_id
                    FROM user_groups ug
                    WHERE ug.user_id = current_setting('mnemos.current_user_id', TRUE)
                )
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
