-- ---------------------------------------------------------------------------
-- MNEMOS v3.1 fix: mnemos_version_snapshot() bytea cast on content
--
-- The v2_versioning trigger computes a content-addressed commit_hash via
--   sha256((NEW.id || '|' || _v || '|' || NEW.content || '|' || NOW()::text)::bytea)
-- That direct text::bytea cast interprets backslash-escape-like sequences
-- (\xHH, \nnn, etc.) as bytea escape syntax. Memories containing `\x`,
-- `\0`, `\r` followed by valid-looking hex/octal digits — which is
-- common in real production content (code snippets, file paths, shell
-- output) — trigger "invalid input syntax for type bytea" and reject
-- the INSERT/UPDATE outright.
--
-- Surfaced during the v3.1 CERBERUS test deployment seeding
-- (2026-04-23). The benchmark drain workaround was to DISABLE the
-- trigger for the bulk load; production installs don't have that
-- luxury. This migration replaces the function body with
-- convert_to(text, 'UTF8') which converts text to its UTF-8 byte
-- representation without trying to parse escape sequences.
--
-- Idempotent: CREATE OR REPLACE FUNCTION replaces the existing
-- definition in place. Safe to re-run. No data changes; triggers
-- stay attached under their existing names.
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
                OLD.content, OLD.category, OLD.subcategory, OLD.metadata,
                OLD.verbatim_content, OLD.owner_id, OLD.namespace, OLD.permission_mode,
                OLD.source_model, OLD.source_provider, OLD.source_session, OLD.source_agent,
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
