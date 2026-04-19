-- =============================================================================
-- MNEMOS v2 Migration — Memory Versioning + GRAEAE Audit Log
-- Fully additive. Idempotent. Wrapped in BEGIN/COMMIT.
-- Run as: sudo -u postgres psql -d mnemos -f migrations_v2_versioning.sql
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. memory_versions table
--    No FK to memories — intentional. Versions survive memory deletion.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_versions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id       TEXT        NOT NULL,
    version_num     INTEGER     NOT NULL,
    content         TEXT        NOT NULL,
    category        TEXT        NOT NULL,
    subcategory     TEXT,
    metadata        JSONB,
    verbatim_content TEXT,
    owner_id        TEXT        NOT NULL,
    namespace       TEXT        NOT NULL,
    permission_mode SMALLINT    NOT NULL,
    source_model    TEXT,
    source_provider TEXT,
    source_session  TEXT,
    source_agent    TEXT,
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    snapshot_by     TEXT,       -- user_id from mnemos.current_user_id session var
    change_type     TEXT        NOT NULL CHECK (change_type IN ('create', 'update', 'delete')),
    UNIQUE (memory_id, version_num)
);

CREATE INDEX IF NOT EXISTS idx_mv_memory_id        ON memory_versions(memory_id);
CREATE INDEX IF NOT EXISTS idx_mv_memory_id_vnum   ON memory_versions(memory_id, version_num DESC);
CREATE INDEX IF NOT EXISTS idx_mv_snapshot_at      ON memory_versions(snapshot_at);

-- ---------------------------------------------------------------------------
-- 2. Trigger function — auto-snapshot on INSERT / meaningful UPDATE / DELETE
--    Now with DAG support: commit_hash, parent_version_id, branch, branch HEAD
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
    _branch := NULLIF(current_setting('mnemos.current_branch', TRUE), '') OR 'main';

    IF TG_OP = 'INSERT' THEN
        -- Create initial version 1
        _commit_hash := encode(sha256((NEW.id || '|1|' || NEW.content || '|' || NOW()::text)::bytea), 'hex');

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

        -- Create/update branch HEAD
        INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
        VALUES (NEW.id, _branch, _new_version_id, _by)
        ON CONFLICT (memory_id, name) DO UPDATE
        SET head_version_id = EXCLUDED.head_version_id;

    ELSIF TG_OP = 'UPDATE' THEN
        -- Only snapshot if meaningful fields changed
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

            -- Get parent (current HEAD of branch)
            SELECT head_version_id INTO _parent_version
            FROM memory_branches
            WHERE memory_id = NEW.id AND name = _branch;

            -- Compute commit hash
            _commit_hash := encode(
                sha256((NEW.id || '|' || _next_v::text || '|' || NEW.content || '|' || NOW()::text)::bytea),
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

            -- Update branch HEAD
            UPDATE memory_branches
            SET head_version_id = _new_version_id
            WHERE memory_id = NEW.id AND name = _branch;
        END IF;

    ELSIF TG_OP = 'DELETE' THEN
        SELECT COALESCE(MAX(version_num), 0) + 1
        INTO   _next_v
        FROM   memory_versions
        WHERE  memory_id = OLD.id AND branch = _branch;

        -- Get parent (current HEAD of branch)
        SELECT head_version_id INTO _parent_version
        FROM memory_branches
        WHERE memory_id = OLD.id AND name = _branch;

        -- Compute commit hash
        _commit_hash := encode(
            sha256((OLD.id || '|' || _next_v::text || '|' || OLD.content || '|' || NOW()::text)::bytea),
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
        ) RETURNING id INTO _new_version_id;

        -- Update branch HEAD
        UPDATE memory_branches
        SET head_version_id = _new_version_id
        WHERE memory_id = OLD.id AND name = _branch;

    END IF;

    RETURN NULL;  -- AFTER trigger; return value ignored
END;
$$;

-- Attach triggers (idempotent via DROP IF EXISTS first)
DROP TRIGGER IF EXISTS trg_memory_version_insert ON memories;
CREATE TRIGGER trg_memory_version_insert
    AFTER INSERT ON memories
    FOR EACH ROW EXECUTE FUNCTION mnemos_version_snapshot();

DROP TRIGGER IF EXISTS trg_memory_version_update ON memories;
CREATE TRIGGER trg_memory_version_update
    AFTER UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION mnemos_version_snapshot();

DROP TRIGGER IF EXISTS trg_memory_version_delete ON memories;
CREATE TRIGGER trg_memory_version_delete
    AFTER DELETE ON memories
    FOR EACH ROW EXECUTE FUNCTION mnemos_version_snapshot();

-- ---------------------------------------------------------------------------
-- 3. Backfill version 1 for all existing memories (idempotent)
--    Includes DAG columns if they exist (migrations_v3_dag.sql idempotent)
-- ---------------------------------------------------------------------------
INSERT INTO memory_versions (
    memory_id, version_num, content, category, subcategory, metadata,
    verbatim_content, owner_id, namespace, permission_mode,
    source_model, source_provider, source_session, source_agent,
    snapshot_by, change_type, branch, commit_hash
)
SELECT
    id, 1, content, category, subcategory, metadata,
    verbatim_content, owner_id, namespace, permission_mode,
    source_model, source_provider, source_session, source_agent,
    'migration', 'create',
    'main',
    encode(sha256((id || '|1|' || content || '|' || NOW()::text)::bytea), 'hex')
FROM memories
WHERE id NOT IN (SELECT memory_id FROM memory_versions)
ON CONFLICT (memory_id, version_num) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. GRAEAE audit log — SHA-256 hash-chained, append-only
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS graeae_audit_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence_num    BIGINT      GENERATED ALWAYS AS IDENTITY,
    consultation_id UUID,       -- soft ref to graeae_consultations.id (no FK — audit log is immutable)
    prompt_hash     TEXT        NOT NULL,   -- SHA-256(prompt)
    response_hash   TEXT        NOT NULL,   -- SHA-256(consensus_response)
    chain_hash      TEXT        NOT NULL,   -- SHA-256(prev_chain_hash || response_hash)
    prev_id         UUID,       -- soft ref to previous graeae_audit_log.id
    task_type       TEXT,
    provider        TEXT,
    quality_score   FLOAT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_sequence ON graeae_audit_log(sequence_num);
CREATE INDEX IF NOT EXISTS idx_audit_created  ON graeae_audit_log(created_at);

-- ---------------------------------------------------------------------------
-- 5. Grants
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON memory_versions  TO mnemos_user;
GRANT SELECT, INSERT ON graeae_audit_log TO mnemos_user;

COMMIT;
