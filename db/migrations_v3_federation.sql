-- ---------------------------------------------------------------------------
-- MNEMOS v3.0.0 migration: cross-instance memory federation (pull-based)
--
-- A peer is a remote MNEMOS instance we pull memories from. Authentication
-- uses a bearer token issued by the remote peer (their admin creates a
-- user with role='federation' and an API key, hands it over, we store it).
--
-- Federated memories are stored locally with id prefix 'fed:{peer_name}:{remote_id}'
-- and federation_source = peer_name, read-only by application convention.
-- ---------------------------------------------------------------------------

-- Registry of remote peers we pull from.
CREATE TABLE IF NOT EXISTS federation_peers (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT         UNIQUE NOT NULL,                 -- 'peer-b', sanitized (alnum+dash)
    base_url            TEXT         NOT NULL,                        -- https://peer.example.com
    auth_token          TEXT         NOT NULL,                        -- Bearer token peer issued to us
    namespace_filter    TEXT[],                                        -- NULL = pull all namespaces
    category_filter     TEXT[],                                        -- NULL = pull all categories
    enabled             BOOLEAN      NOT NULL DEFAULT TRUE,
    sync_interval_secs  INTEGER      NOT NULL DEFAULT 300,
    last_sync_at        TIMESTAMPTZ,
    last_sync_cursor    TIMESTAMPTZ,                                   -- cursor = updated TS of last memory pulled
    last_error          TEXT,
    last_error_at       TIMESTAMPTZ,
    total_pulled        BIGINT       NOT NULL DEFAULT 0,
    created             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT federation_peer_name_format CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$'),
    CONSTRAINT federation_peer_url_format CHECK (base_url LIKE 'http://%' OR base_url LIKE 'https://%'),
    CONSTRAINT federation_peer_interval_min CHECK (sync_interval_secs >= 30)
);

CREATE INDEX IF NOT EXISTS idx_federation_peers_enabled
    ON federation_peers(enabled, last_sync_at) WHERE enabled;

-- Sync log: one row per pull attempt.
CREATE TABLE IF NOT EXISTS federation_sync_log (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    peer_id           UUID         NOT NULL REFERENCES federation_peers(id) ON DELETE CASCADE,
    started_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    memories_pulled   INTEGER      NOT NULL DEFAULT 0,
    memories_new      INTEGER      NOT NULL DEFAULT 0,
    memories_updated  INTEGER      NOT NULL DEFAULT 0,
    error             TEXT,
    cursor_before     TIMESTAMPTZ,
    cursor_after      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_federation_sync_log_peer
    ON federation_sync_log(peer_id, started_at DESC);

-- Tag on memories to track federation origin.
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS federation_source TEXT;

CREATE INDEX IF NOT EXISTS idx_memories_federation
    ON memories(federation_source) WHERE federation_source IS NOT NULL;

-- Keep the remote's updated timestamp so we can dedupe on re-pull.
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS federation_remote_updated TIMESTAMPTZ;

-- (Intentionally not adding a role CHECK constraint on users.role — new 'federation'
-- value is additive. Existing (user/root) clients remain valid.)
