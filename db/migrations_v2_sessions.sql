-- Session Management Schema (Phase 0 extension)
-- Enables server-side conversation history and persistent memory context

BEGIN;

-- 1. Sessions table: tracks conversation state
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
    user_id         TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '7 days'),
    model           TEXT        NOT NULL DEFAULT 'gpt-4o',
    compression_tier INT        NOT NULL DEFAULT 1,  -- 1=LETHE, 2=ALETHEIA, 3=ANAMNESIS
    message_count   INT         NOT NULL DEFAULT 0,
    total_tokens    INT         NOT NULL DEFAULT 0,
    metadata        JSONB       DEFAULT NULL,
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_sessions_user_created ON sessions(user_id, created_at DESC);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

-- 2. Session messages table: conversation history
CREATE TABLE IF NOT EXISTS session_messages (
    id              TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
    session_id      TEXT        NOT NULL,
    message_id      TEXT        NOT NULL DEFAULT gen_random_uuid()::text,
    role            TEXT        NOT NULL,  -- "user", "assistant", "system"
    content         TEXT        NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model           TEXT,
    tokens_used     INT,
    memories_injected INT       DEFAULT 0,
    compression_ratio FLOAT,
    metadata        JSONB       DEFAULT NULL,
    CONSTRAINT fk_session_messages_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_session_messages_session_ts ON session_messages(session_id, timestamp ASC);
CREATE INDEX idx_session_messages_role ON session_messages(session_id, role);

-- 3. Session context injection table: tracks which memories were injected per turn
CREATE TABLE IF NOT EXISTS session_memory_injections (
    id              TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
    session_id      TEXT        NOT NULL,
    message_id      TEXT        NOT NULL,
    memory_id       TEXT        NOT NULL,
    relevance_score FLOAT,
    compressed      BOOLEAN     DEFAULT TRUE,
    compression_ratio FLOAT,
    injection_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_session_memory_injections_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    CONSTRAINT fk_session_memory_injections_memory FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE SET NULL
);

CREATE INDEX idx_session_memory_injections_session ON session_memory_injections(session_id);

COMMIT;
