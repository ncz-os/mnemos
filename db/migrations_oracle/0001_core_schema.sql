-- Oracle 26ai / 23c core MNEMOS schema bootstrap
-- Generated with Codex assistance for Oracle port M7
-- Minimal but sufficient for CHARON import + sidecars

CREATE TABLE IF NOT EXISTS users (
    id VARCHAR2(100) PRIMARY KEY,
    display_name VARCHAR2(200),
    email VARCHAR2(200),
    role VARCHAR2(50) DEFAULT 'user',
    namespace VARCHAR2(100) DEFAULT 'default',
    created_at DATE DEFAULT SYSDATE
);

CREATE TABLE IF NOT EXISTS memories (
    id VARCHAR2(100) PRIMARY KEY,
    content CLOB,
    category VARCHAR2(100),
    subcategory VARCHAR2(100),
    metadata CLOB,
    quality_rating NUMBER(5,2) DEFAULT 50,
    compressed_content CLOB,
    verbatim_content CLOB,
    owner_id VARCHAR2(100) DEFAULT 'default',
    namespace VARCHAR2(100) DEFAULT 'default',
    created_at DATE DEFAULT SYSDATE,
    updated_at DATE DEFAULT SYSDATE,
    external_id VARCHAR2(200)
);

-- Idempotent per-column ALTERs: bare `ALTER TABLE ADD (...)` fails ORA-01430
-- on re-run. PL/SQL guard adds each column only when missing so the
-- migration is safe to replay.
DECLARE
    v_count NUMBER;
    PROCEDURE add_col(p_col VARCHAR2, p_ddl VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count
          FROM user_tab_columns
         WHERE table_name = 'MEMORIES'
           AND column_name = UPPER(p_col);
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE memories ADD (' || p_ddl || ')';
        END IF;
    END;
BEGIN
    add_col('created',         'created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL');
    add_col('updated',         'updated TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL');
    add_col('permission_mode', 'permission_mode NUMBER(4) DEFAULT 600 NOT NULL');
    add_col('source_model',    'source_model VARCHAR2(200)');
    add_col('source_provider', 'source_provider VARCHAR2(100)');
    add_col('source_session',  'source_session VARCHAR2(200)');
    add_col('source_agent',    'source_agent VARCHAR2(200)');
    add_col('group_id',        'group_id VARCHAR2(100)');
    add_col('archived_at',     'archived_at TIMESTAMP WITH TIME ZONE');
    add_col('deleted_at',      'deleted_at TIMESTAMP WITH TIME ZONE');
END;
/

-- Backfill legacy created_at/updated_at into the new TSTZ columns.
-- Safe to re-run: rows with already-populated created/updated keep
-- their values via COALESCE; ORA-00904 is suppressed when the legacy
-- columns are absent on fresh installs.
DECLARE
    v_has_created_at NUMBER;
    v_has_updated_at NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_has_created_at FROM user_tab_columns
     WHERE table_name = 'MEMORIES' AND column_name = 'CREATED_AT';
    SELECT COUNT(*) INTO v_has_updated_at FROM user_tab_columns
     WHERE table_name = 'MEMORIES' AND column_name = 'UPDATED_AT';
    IF v_has_created_at > 0 AND v_has_updated_at > 0 THEN
        EXECUTE IMMEDIATE '
            UPDATE memories
               SET created = COALESCE(CAST(created_at AS TIMESTAMP WITH TIME ZONE), created),
                   updated = COALESCE(CAST(updated_at AS TIMESTAMP WITH TIME ZONE), updated)
             WHERE created_at IS NOT NULL OR updated_at IS NOT NULL';
    END IF;
END;
/

CREATE INDEX IF NOT EXISTS idx_memories_live_owner_ns
    ON memories(owner_id, namespace, deleted_at);

CREATE INDEX IF NOT EXISTS idx_memories_owner_ns ON memories(owner_id, namespace);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_list_created
    ON memories(deleted_at, created DESC, id);
CREATE INDEX IF NOT EXISTS idx_memories_owner_ns_created
    ON memories(owner_id, namespace, deleted_at, archived_at, created DESC, id);

CREATE TABLE IF NOT EXISTS memory_versions (
    id VARCHAR2(100) PRIMARY KEY,
    memory_id VARCHAR2(100) NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    version_num NUMBER,
    content CLOB NOT NULL,
    category VARCHAR2(100),
    subcategory VARCHAR2(100),
    metadata CLOB CHECK (metadata IS JSON),
    verbatim_content CLOB,
    owner_id VARCHAR2(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR2(100) DEFAULT 'default' NOT NULL,
    permission_mode NUMBER(4) DEFAULT 600,
    source_model VARCHAR2(200),
    source_provider VARCHAR2(100),
    source_session VARCHAR2(200),
    source_agent VARCHAR2(200),
    snapshot_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP,
    snapshot_by VARCHAR2(100),
    change_type VARCHAR2(40) DEFAULT 'create',
    commit_hash VARCHAR2(128),
    parent_version_id VARCHAR2(100),
    branch VARCHAR2(100) DEFAULT 'main',
    merge_parents CLOB CHECK (merge_parents IS JSON),
    deleted_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_mv_memory_id ON memory_versions(memory_id);
CREATE INDEX IF NOT EXISTS idx_mv_memory_id_vnum ON memory_versions(memory_id, version_num DESC);
CREATE INDEX IF NOT EXISTS idx_mv_snapshot_at ON memory_versions(snapshot_at);
CREATE INDEX IF NOT EXISTS idx_mv_commit_hash ON memory_versions(commit_hash);
CREATE INDEX IF NOT EXISTS idx_mv_branch_head ON memory_versions(memory_id, branch, version_num DESC);
CREATE INDEX IF NOT EXISTS idx_mv_owner_namespace ON memory_versions(owner_id, namespace);
CREATE INDEX IF NOT EXISTS idx_mv_parent_version ON memory_versions(parent_version_id);
CREATE INDEX IF NOT EXISTS idx_mv_deleted ON memory_versions(deleted_at);


CREATE TABLE IF NOT EXISTS kg_triples (
    id VARCHAR2(100) PRIMARY KEY,
    subject VARCHAR2(1000) NOT NULL,
    predicate VARCHAR2(500) NOT NULL,
    object CLOB NOT NULL,
    subject_type VARCHAR2(100),
    object_type VARCHAR2(100),
    valid_from TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP,
    valid_until TIMESTAMP WITH TIME ZONE,
    memory_id VARCHAR2(100),
    confidence NUMBER DEFAULT 1.0 NOT NULL,
    metadata CLOB CHECK (metadata IS JSON),
    created DATE DEFAULT SYSDATE NOT NULL,
    owner_id VARCHAR2(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR2(100) DEFAULT 'default' NOT NULL,
    extracted_by_run_id VARCHAR2(100),
    deleted_at TIMESTAMP WITH TIME ZONE
);


CREATE TABLE IF NOT EXISTS compression_manifest (
    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    memory_id VARCHAR2(100),
    tier NUMBER,
    ratio NUMBER,
    model VARCHAR2(100),
    created_at DATE DEFAULT SYSDATE
);

CREATE TABLE IF NOT EXISTS deletion_log (
    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    memory_id VARCHAR2(100),
    requested_at DATE DEFAULT SYSDATE,
    executed_at DATE,
    status VARCHAR2(20)
);

-- Sessions & related (minimal)
CREATE TABLE IF NOT EXISTS sessions (
    id VARCHAR2(100) PRIMARY KEY,
    user_id VARCHAR2(100),
    created_at DATE DEFAULT SYSDATE
);

CREATE TABLE IF NOT EXISTS session_messages (
    id NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    session_id VARCHAR2(100),
    role VARCHAR2(20),
    content CLOB,
    created_at DATE DEFAULT SYSDATE
);

-- ────────────────────────────────────────────────────────────────────────────
-- Sidecar tables (M7 P1.3 — boot + memory CRUD parity)
-- ────────────────────────────────────────────────────────────────────────────

-- memory_branches: head-version pointers for memory DAG branches.
-- Referenced by OracleMemoryRepository.upsert_memory_branch_head and
-- BranchRepository surface. UNIQUE (memory_id, name) enforces the
-- MERGE upsert semantics used by the repo.
CREATE TABLE IF NOT EXISTS memory_branches (
    id VARCHAR2(100) PRIMARY KEY,
    memory_id VARCHAR2(100) NOT NULL,
    name VARCHAR2(100) NOT NULL,
    head_version_id VARCHAR2(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    created_by VARCHAR2(100),
    CONSTRAINT uq_memory_branches UNIQUE (memory_id, name)
);
CREATE INDEX IF NOT EXISTS idx_memory_branches_memory ON memory_branches(memory_id);

-- state: backend-neutral key/value store. Postgres uses table name
-- "state" (not "state_kv"); mirror that for parity.
CREATE TABLE IF NOT EXISTS state (
    owner_id VARCHAR2(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR2(100) DEFAULT 'default' NOT NULL,
    key VARCHAR2(500) NOT NULL,
    value CLOB,
    updated TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    version NUMBER DEFAULT 1 NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_state PRIMARY KEY (owner_id, namespace, key)
);

-- federation_peers: scanned at startup (_log_federation_startup_guidance).
-- Boot is best-effort; absent table is tolerated, but having it avoids the
-- log warning and unblocks federation slice.
CREATE TABLE IF NOT EXISTS federation_peers (
    id VARCHAR2(100) PRIMARY KEY,
    name VARCHAR2(100) UNIQUE NOT NULL,
    base_url VARCHAR2(500) NOT NULL,
    auth_token VARCHAR2(500) NOT NULL,
    namespace_filter CLOB,
    category_filter CLOB,
    enabled NUMBER(1) DEFAULT 1 NOT NULL,
    sync_interval_secs NUMBER DEFAULT 300 NOT NULL,
    last_sync_at TIMESTAMP WITH TIME ZONE,
    last_sync_cursor TIMESTAMP WITH TIME ZONE,
    last_error CLOB,
    last_error_at TIMESTAMP WITH TIME ZONE,
    total_pulled NUMBER DEFAULT 0 NOT NULL,
    compat_mode VARCHAR2(20) DEFAULT 'strict' NOT NULL,
    peer_mnemos_version VARCHAR2(50),
    last_schema_check_at TIMESTAMP WITH TIME ZONE,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT federation_peer_interval_min CHECK (sync_interval_secs >= 30)
);
CREATE INDEX IF NOT EXISTS idx_federation_peers_enabled ON federation_peers(enabled, last_sync_at);

-- webhook_subscriptions: outbound webhook surface.
CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id VARCHAR2(100) PRIMARY KEY,
    url VARCHAR2(1000) NOT NULL,
    events CLOB DEFAULT '[]',
    secret VARCHAR2(500),
    owner_id VARCHAR2(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR2(100) DEFAULT 'default' NOT NULL,
    revoked NUMBER(1) DEFAULT 0 NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_subs_owner ON webhook_subscriptions(owner_id, namespace);

-- memory_compression_candidates: per-engine compression-contest entries.
CREATE TABLE IF NOT EXISTS memory_compression_candidates (
    id VARCHAR2(100) PRIMARY KEY,
    memory_id VARCHAR2(100) NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    owner_id VARCHAR2(100) DEFAULT 'default' NOT NULL,
    contest_id VARCHAR2(100),
    engine_id VARCHAR2(100) NOT NULL,
    engine_version VARCHAR2(50),
    candidate_content CLOB,
    candidate_tokens NUMBER,
    compression_ratio NUMBER,
    quality_score NUMBER,
    composite_score NUMBER,
    is_winner NUMBER(1) DEFAULT 0 NOT NULL,
    reject_reason CLOB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcc_memory ON memory_compression_candidates(memory_id);
CREATE INDEX IF NOT EXISTS idx_mcc_contest ON memory_compression_candidates(contest_id);
CREATE INDEX IF NOT EXISTS idx_mcc_memory_winner ON memory_compression_candidates(memory_id, is_winner);

-- memory_compressed_variants: winning compression per memory.
CREATE TABLE IF NOT EXISTS memory_compressed_variants (
    memory_id VARCHAR2(100) PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    owner_id VARCHAR2(100) DEFAULT 'default' NOT NULL,
    winner_candidate_id VARCHAR2(100) REFERENCES memory_compression_candidates(id) ON DELETE SET NULL,
    engine_id VARCHAR2(100) NOT NULL,
    engine_version VARCHAR2(50),
    compressed_content CLOB,
    compressed_tokens NUMBER,
    compression_ratio NUMBER,
    quality_score NUMBER,
    composite_score NUMBER,
    scoring_profile VARCHAR2(50) DEFAULT 'balanced' NOT NULL,
    judge_model VARCHAR2(200),
    selected_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcv_owner ON memory_compressed_variants(owner_id);
CREATE INDEX IF NOT EXISTS idx_mcv_engine ON memory_compressed_variants(engine_id);

-- federation_sync_log: one row per pull attempt.
CREATE TABLE IF NOT EXISTS federation_sync_log (
    id VARCHAR2(100) PRIMARY KEY,
    peer_id VARCHAR2(100) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    memories_pulled NUMBER DEFAULT 0 NOT NULL,
    memories_new NUMBER DEFAULT 0 NOT NULL,
    memories_updated NUMBER DEFAULT 0 NOT NULL,
    error CLOB,
    cursor_before TIMESTAMP WITH TIME ZONE,
    cursor_after TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_fed_sync_log_peer ON federation_sync_log(peer_id, started_at DESC);

-- federation_consolidation_tombstones: track remote→local canonical
-- consolidation events so re-pulls do not resurrect collapsed rows.
CREATE TABLE IF NOT EXISTS federation_consolidation_tombstones (
    peer_name VARCHAR2(100) NOT NULL,
    remote_id VARCHAR2(100) NOT NULL,
    local_id VARCHAR2(100) NOT NULL,
    local_canonical_id VARCHAR2(100) NOT NULL,
    canonical_remote_id VARCHAR2(100) NOT NULL,
    consolidated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_fed_tomb PRIMARY KEY (peer_name, remote_id)
);

-- webhook_deliveries: outbox queue for webhook_subscriptions.
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id VARCHAR2(100) PRIMARY KEY,
    subscription_id VARCHAR2(100) NOT NULL,
    event_type VARCHAR2(100) NOT NULL,
    payload CLOB,
    owner_id VARCHAR2(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR2(100) DEFAULT 'default' NOT NULL,
    state VARCHAR2(40) DEFAULT 'pending' NOT NULL,
    attempt_count NUMBER DEFAULT 0 NOT NULL,
    next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    last_error CLOB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_del_state ON webhook_deliveries(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_webhook_del_sub ON webhook_deliveries(subscription_id);

-- Additional memories columns: federation marker + recall counters +
-- content hash. Wrapped in the same PL/SQL guard pattern used for the
-- v1 column additions so replays are safe.
DECLARE
    v_count NUMBER;
    PROCEDURE add_col(p_col VARCHAR2, p_ddl VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count
          FROM user_tab_columns
         WHERE table_name = 'MEMORIES'
           AND column_name = UPPER(p_col);
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE memories ADD (' || p_ddl || ')';
        END IF;
    END;
BEGIN
    add_col('federation_source',          'federation_source VARCHAR2(200)');
    add_col('federation_remote_updated',  'federation_remote_updated TIMESTAMP WITH TIME ZONE');
    add_col('recall_count',               'recall_count NUMBER DEFAULT 0 NOT NULL');
    add_col('last_recalled_at',           'last_recalled_at TIMESTAMP WITH TIME ZONE');
    add_col('content_hash',               'content_hash VARCHAR2(64)');
    -- Oracle 23ai VECTOR for native cosine similarity (semantic_search).
    -- Variable-dim so the same column supports 384/768/1024/1536/3072
    -- embedding models. FLOAT32 matches the python-oracledb wire format.
    add_col('embedding',                  'embedding VECTOR(*, FLOAT32)');
END;
/

CREATE INDEX IF NOT EXISTS idx_memories_fed ON memories(federation_source);
CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(content_hash);
CREATE INDEX IF NOT EXISTS idx_memories_feed_cursor
    ON memories(federation_source, deleted_at, archived_at, updated, id);
CREATE INDEX IF NOT EXISTS idx_memories_updated_cursor
    ON memories(deleted_at, updated, id);
