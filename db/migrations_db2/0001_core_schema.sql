-- IBM Db2 12.1.x core MNEMOS schema bootstrap (NATIVE Db2 — no ORA-compat).
-- migration_id: db2/0001_core_schema
-- depends_on: (none)
--
-- This migration uses Db2-native types (VARCHAR, DECIMAL, BIGINT, TIMESTAMP).
-- It does NOT require ENABLE_ORACLE_COMPATIBILITY=true. The Db2-native
-- repository layer in mnemos/persistence/db2.py emits only Db2-native SQL
-- (? positional binds, CURRENT TIMESTAMP, FROM SYSIBM.SYSDUMMY1, etc.) — see
-- docs/native-db2-port-plan.md §1.
--
-- Statement terminator: @ (Db2 CLP convention — scripts/db2_apply_migration.py
-- splits on @ regardless of --#SET TERMINATOR directives).
--
-- Most statements ported from db/migrations_oracle/0001_core_schema.sql with
-- Oracle-compat aliases replaced by Db2-native types:
--   * Oracle CHAR-varying types → VARCHAR(N)
--   * Oracle generic-numeric (counters) → BIGINT / INTEGER
--   * Oracle fixed-precision numeric → SMALLINT / INTEGER
--   * Oracle numeric (fractional / metrics) → DOUBLE
--   * CLOB stays CLOB (Db2 native)
--   * DATE DEFAULT CURRENT DATE stays (Db2 calendar-date, already native)
--   * TIMESTAMP DEFAULT CURRENT TIMESTAMP stays (already native)
--   * Db2 12.1.2+ ships VECTOR; dimension {{embedding_dim}} is template-substituted
--     by scripts/db2_apply_migration.py (default 768 for nomic-embed-text).
--   * CREATE VECTOR INDEX … WITH DISTANCE EUCLIDEAN (Db2 12.1.5 EAP DiskANN;
--     tolerated as benign-skip on 12.1.4 / non-EAP builds).

CREATE TABLE memories (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    content CLOB,
    category VARCHAR(100),
    subcategory VARCHAR(100),
    metadata CLOB,
    quality_rating DECIMAL(5,2) DEFAULT 50,
    compressed_content CLOB,
    verbatim_content CLOB,
    owner_id VARCHAR(100) DEFAULT 'default',
    namespace VARCHAR(100) DEFAULT 'default',
    created_at DATE DEFAULT CURRENT DATE,
    updated_at DATE DEFAULT CURRENT DATE,
    external_id VARCHAR(200),
    created TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    updated TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    permission_mode SMALLINT DEFAULT 600 NOT NULL,
    source_model VARCHAR(200),
    source_provider VARCHAR(100),
    source_session VARCHAR(200),
    source_agent VARCHAR(200),
    group_id VARCHAR(100),
    archived_at TIMESTAMP,
    deleted_at TIMESTAMP,
    federation_source VARCHAR(200),
    federation_remote_updated TIMESTAMP,
    recall_count BIGINT DEFAULT 0 NOT NULL,
    last_recalled_at TIMESTAMP,
    content_hash VARCHAR(64),
    embedding VECTOR({{embedding_dim}}, FLOAT32)
)@

CREATE INDEX idx_memories_live_owner_ns
    ON memories(owner_id, namespace, deleted_at)@

CREATE INDEX idx_memories_category ON memories(category)@
CREATE INDEX idx_memories_fed ON memories(federation_source)@
CREATE INDEX idx_memories_content_hash ON memories(content_hash)@
CREATE INDEX idx_memories_updated_cursor
    ON memories(deleted_at, updated, id)@
CREATE INDEX idx_memories_feed_cursor
    ON memories(federation_source, deleted_at, archived_at, updated, id)@

-- VECTOR index — Db2 12.1.5 ships DiskANN. Db2 12.1.4 has no native
-- ANN index (linear scan). The 12.1.5 EAP syntax (verified against
-- scripts/bench_v4.py::bench_db2) is:
--     CREATE VECTOR INDEX <name> ON <table>(<col>) WITH DISTANCE EUCLIDEAN
-- EUCLIDEAN preserves COSINE top-K ordering for L2-normalized
-- embeddings (MNEMOS default) — see mnemos/persistence/db2.py
-- module-level note on _resolve_db2_vector_index_mode.
--
-- This statement requires DB2_VECTOR_INDEXING=YES at instance level:
--     db2set DB2_VECTOR_INDEXING=YES && db2stop && db2start
-- The Db2Backend startup probe in mnemos/persistence/db2.py emits a
-- WARNING when this registry var is missing.
--
-- The db2_apply_migration.py runner is expected to tolerate failure on
-- this statement on 12.1.4 / non-EAP builds. Without the index, the
-- app-path semantic_search override at MNEMOS_DB2_VECTOR_INDEX=approx
-- still returns correct results (sequential scan) but with EAP-only
-- latency characteristics; set MNEMOS_DB2_VECTOR_INDEX=exact to
-- communicate intent on non-EAP builds.
--
-- Build-time tuning (Db2 12.1.5 EAP; validated on pegasus db2-mnemos
-- 2026-06-22 against ~12.6k x 768-dim rows: ~11% faster execute, build ~1s,
-- recall intact. Ref: IBM community blog "Vector Indexes in Db2", 2026-06-19):
--   * PCT_COMP_VECT_SIZE — compressed-vector ratio (percent). Higher trades a
--     little memory for lower query latency; 15 gives ~8-11% faster search vs
--     the 5% default at a still-tiny footprint for MNEMOS-scale corpora.
--   * BUILD_PARALLELISM — index-construction worker threads (~75% of cores).
--   * BUILD_MEM_BUDGET — construction memory budget in GB (less build-time I/O).
-- Values are template-substituted by db2_apply_migration.py with host-adaptive
-- defaults (CLI-overridable). On 12.1.4 / non-EAP the whole statement fails and
-- the runner tolerates it (as before). Query-time SEARCH_LIST_SIZE /
-- SEARCH_BEAM_WIDTH are intentionally NOT emitted — unsupported on this 12.1.5
-- EAP build (SQL0104N); they are a later-build feature.
CREATE VECTOR INDEX idx_memories_emb_diskann
    ON memories(embedding) WITH DISTANCE EUCLIDEAN
    PCT_COMP_VECT_SIZE {{vector_pct_comp}}
    BUILD_PARALLELISM {{vector_build_parallelism}}
    BUILD_MEM_BUDGET {{vector_build_mem_budget}} @

CREATE TABLE memory_versions (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    memory_id VARCHAR(100) NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    version_num INTEGER,
    content CLOB NOT NULL,
    category VARCHAR(100),
    subcategory VARCHAR(100),
    metadata CLOB,
    verbatim_content CLOB,
    owner_id VARCHAR(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR(100) DEFAULT 'default' NOT NULL,
    permission_mode SMALLINT DEFAULT 600,
    source_model VARCHAR(200),
    source_provider VARCHAR(100),
    source_session VARCHAR(200),
    source_agent VARCHAR(200),
    snapshot_at TIMESTAMP DEFAULT CURRENT TIMESTAMP,
    snapshot_by VARCHAR(100),
    change_type VARCHAR(40) DEFAULT 'create',
    commit_hash VARCHAR(128),
    parent_version_id VARCHAR(100),
    branch VARCHAR(100) DEFAULT 'main',
    merge_parents CLOB,
    deleted_at TIMESTAMP
)@

CREATE INDEX idx_mv_memory_id ON memory_versions(memory_id)@
CREATE INDEX idx_mv_branch_head ON memory_versions(memory_id, branch, version_num DESC)@

CREATE TABLE kg_triples (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    subject VARCHAR(1000) NOT NULL,
    predicate VARCHAR(500) NOT NULL,
    object CLOB NOT NULL,
    subject_type VARCHAR(100),
    object_type VARCHAR(100),
    valid_from TIMESTAMP DEFAULT CURRENT TIMESTAMP,
    valid_until TIMESTAMP,
    memory_id VARCHAR(100),
    confidence DOUBLE DEFAULT 1.0 NOT NULL,
    metadata CLOB,
    created DATE DEFAULT CURRENT DATE NOT NULL,
    owner_id VARCHAR(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR(100) DEFAULT 'default' NOT NULL,
    deleted_at TIMESTAMP
)@

CREATE TABLE memory_branches (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    memory_id VARCHAR(100) NOT NULL,
    name VARCHAR(100) NOT NULL,
    head_version_id VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    created_by VARCHAR(100),
    CONSTRAINT uq_memory_branches UNIQUE (memory_id, name)
)@
CREATE INDEX idx_memory_branches_memory ON memory_branches(memory_id)@

CREATE TABLE state (
    owner_id VARCHAR(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR(100) DEFAULT 'default' NOT NULL,
    key VARCHAR(500) NOT NULL,
    value CLOB,
    updated TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    version INTEGER DEFAULT 1 NOT NULL,
    deleted_at TIMESTAMP,
    CONSTRAINT pk_state PRIMARY KEY (owner_id, namespace, key)
)@

CREATE TABLE federation_peers (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    base_url VARCHAR(500) NOT NULL,
    auth_token VARCHAR(500) NOT NULL,
    namespace_filter CLOB,
    category_filter CLOB,
    enabled SMALLINT DEFAULT 1 NOT NULL,
    sync_interval_secs INTEGER DEFAULT 300 NOT NULL,
    last_sync_at TIMESTAMP,
    last_sync_cursor TIMESTAMP,
    last_error CLOB,
    last_error_at TIMESTAMP,
    total_pulled BIGINT DEFAULT 0 NOT NULL,
    compat_mode VARCHAR(20) DEFAULT 'strict' NOT NULL,
    peer_mnemos_version VARCHAR(50),
    last_schema_check_at TIMESTAMP,
    created TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    updated TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    CONSTRAINT federation_peer_interval_min CHECK (sync_interval_secs >= 30)
)@

CREATE TABLE federation_sync_log (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    peer_id VARCHAR(100) NOT NULL,
    started_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    memories_pulled BIGINT DEFAULT 0 NOT NULL,
    memories_new BIGINT DEFAULT 0 NOT NULL,
    memories_updated BIGINT DEFAULT 0 NOT NULL,
    error CLOB,
    cursor_before TIMESTAMP,
    cursor_after TIMESTAMP
)@

CREATE TABLE federation_consolidation_tombstones (
    peer_name VARCHAR(100) NOT NULL,
    remote_id VARCHAR(100) NOT NULL,
    local_id VARCHAR(100) NOT NULL,
    local_canonical_id VARCHAR(100) NOT NULL,
    canonical_remote_id VARCHAR(100) NOT NULL,
    consolidated_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    CONSTRAINT pk_fed_tomb PRIMARY KEY (peer_name, remote_id)
)@

CREATE TABLE webhook_subscriptions (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    url VARCHAR(1000) NOT NULL,
    events CLOB DEFAULT '[]',
    secret VARCHAR(500),
    owner_id VARCHAR(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR(100) DEFAULT 'default' NOT NULL,
    revoked SMALLINT DEFAULT 0 NOT NULL,
    revoked_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL
)@

CREATE TABLE webhook_deliveries (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    subscription_id VARCHAR(100) NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    payload CLOB,
    owner_id VARCHAR(100) DEFAULT 'default' NOT NULL,
    namespace VARCHAR(100) DEFAULT 'default' NOT NULL,
    state VARCHAR(40) DEFAULT 'pending' NOT NULL,
    attempt_count BIGINT DEFAULT 0 NOT NULL,
    next_attempt_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    last_error CLOB,
    created_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL
)@

CREATE TABLE memory_compression_candidates (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    memory_id VARCHAR(100) NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    owner_id VARCHAR(100) DEFAULT 'default' NOT NULL,
    contest_id VARCHAR(100),
    engine_id VARCHAR(100) NOT NULL,
    engine_version VARCHAR(50),
    candidate_content CLOB,
    candidate_tokens INTEGER,
    compression_ratio DOUBLE,
    quality_score DOUBLE,
    composite_score DOUBLE,
    is_winner SMALLINT DEFAULT 0 NOT NULL,
    reject_reason CLOB,
    created_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL
)@

CREATE TABLE memory_compressed_variants (
    memory_id VARCHAR(100) NOT NULL PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    owner_id VARCHAR(100) DEFAULT 'default' NOT NULL,
    winner_candidate_id VARCHAR(100) REFERENCES memory_compression_candidates(id) ON DELETE SET NULL,
    engine_id VARCHAR(100) NOT NULL,
    engine_version VARCHAR(50),
    compressed_content CLOB,
    compressed_tokens INTEGER,
    compression_ratio DOUBLE,
    quality_score DOUBLE,
    composite_score DOUBLE,
    scoring_profile VARCHAR(50) DEFAULT 'balanced' NOT NULL,
    judge_model VARCHAR(200),
    selected_at TIMESTAMP DEFAULT CURRENT TIMESTAMP NOT NULL
)@

-- ────────────────────────────────────────────────────────────────────────────
-- Oracle-parity tables (Opus review O7 / Codex B3). Ported 2026-05-20.
-- These five tables exist in db/migrations_oracle/0001_core_schema.sql and
-- are required for: session API surface, deletion ledger / GDPR trail,
-- compression manifest admin, user identity. Without them the
-- corresponding routes raise SQL0204N on Db2 deployments.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE users (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    display_name VARCHAR(200),
    email VARCHAR(200),
    role VARCHAR(50) DEFAULT 'user',
    namespace VARCHAR(100) DEFAULT 'default',
    created_at DATE DEFAULT CURRENT DATE
)@

CREATE TABLE sessions (
    id VARCHAR(100) NOT NULL PRIMARY KEY,
    user_id VARCHAR(100),
    created_at DATE DEFAULT CURRENT DATE
)@

CREATE TABLE session_messages (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    session_id VARCHAR(100),
    role VARCHAR(20),
    content CLOB,
    created_at DATE DEFAULT CURRENT DATE
)@

CREATE TABLE deletion_log (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    memory_id VARCHAR(100),
    requested_at DATE DEFAULT CURRENT DATE,
    executed_at DATE,
    status VARCHAR(20)
)@

CREATE TABLE compression_manifest (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    memory_id VARCHAR(100),
    tier INTEGER,
    ratio DOUBLE,
    model VARCHAR(100),
    created_at DATE DEFAULT CURRENT DATE
)@
