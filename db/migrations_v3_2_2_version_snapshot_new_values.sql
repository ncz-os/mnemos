-- MNEMOS v3.2.2 — fix mnemos_version_snapshot() to record NEW state on UPDATE
--
-- The v3_1 versioning_fix migration's UPDATE branch inserted OLD.* into
-- memory_versions. Every memory edit produced a version_num row that
-- repeated the PRIOR content instead of capturing the post-update state.
-- Consequence: `GET /v1/memories/{id}/log` and `GET .../commits/{hash}`
-- returned pre-edit snapshots for every version after v1, and a revert
-- to version N actually reverted to version N-1. Diff semantics were
-- likewise shifted by one version.
--
-- Fix: INSERT NEW.* on UPDATE (matches the INSERT branch's convention
-- of "each version row = state AT that commit"). DELETE branch keeps
-- OLD.* since the row no longer exists at that point and the tombstone
-- must preserve the last-known state.
--
-- CREATE OR REPLACE FUNCTION is idempotent; the attached triggers
-- (trg_memory_version_{insert,update,delete}) stay bound. No rewrite
-- of historical memory_versions rows — a backfill would need the full
-- edit log to reconstruct past NEW states, which is not recoverable.
-- Going forward, every UPDATE writes the correct snapshot.

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
            snapshot_by, change_type, commit_hash, branch, parent_version_id
        ) VALUES (
            NEW.id, 1, NEW.content, NEW.category, NEW.subcategory, NEW.metadata,
            NEW.verbatim_content, NEW.owner_id, NEW.namespace, NEW.permission_mode,
            NEW.source_model, NEW.source_provider, NEW.source_session, NEW.source_agent,
            _by, 'create', _commit_hash, _branch, NULL
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
                snapshot_by, change_type, commit_hash, branch, parent_version_id
            ) VALUES (
                NEW.id, _next_v,
                NEW.content, NEW.category, NEW.subcategory, NEW.metadata,
                NEW.verbatim_content, NEW.owner_id, NEW.namespace, NEW.permission_mode,
                NEW.source_model, NEW.source_provider, NEW.source_session, NEW.source_agent,
                _by, 'update', _commit_hash, _branch, _parent_version
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
            snapshot_by, change_type, commit_hash, branch, parent_version_id
        ) VALUES (
            OLD.id, _next_v,
            OLD.content, OLD.category, OLD.subcategory, OLD.metadata,
            OLD.verbatim_content, OLD.owner_id, OLD.namespace, OLD.permission_mode,
            OLD.source_model, OLD.source_provider, OLD.source_session, OLD.source_agent,
            _by, 'delete', _commit_hash, _branch, _parent_version
        );
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$;
