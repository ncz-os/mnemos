-- 0047_sessions_chat_columns.sql — Db2 12.1.5 (Oracle Compat) port.
--
-- Adds the chat-session columns the SessionsRepository expects (matching
-- PostgreSQL v2_sessions) to the minimal sessions/session_messages tables the
-- Db2 0001 core schema created. Db2Backend inherits the Oracle session methods.
-- Additive: existing columns are untouched. Plain ALTER ... ADD COLUMN relies
-- on the migration runner's benign-error handling (SQLSTATE 42711 = duplicate
-- column, 42710 = duplicate index) for replay safety. Mirrors the Oracle 0047
-- sibling; db/migrations/0047 is a no-op parity anchor.

ALTER TABLE sessions ADD COLUMN namespace VARCHAR(100) NOT NULL DEFAULT 'default';
ALTER TABLE sessions ADD COLUMN model VARCHAR(200);
ALTER TABLE sessions ADD COLUMN last_activity TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE sessions ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN compression_tier INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sessions ADD COLUMN deleted_at TIMESTAMP(6) WITH TIME ZONE;

ALTER TABLE session_messages ADD COLUMN message_id VARCHAR(36);
ALTER TABLE session_messages ADD COLUMN model VARCHAR(200);
ALTER TABLE session_messages ADD COLUMN tokens_used INTEGER;
ALTER TABLE session_messages ADD COLUMN memories_injected INTEGER DEFAULT 0;
ALTER TABLE session_messages ADD COLUMN deleted_at TIMESTAMP(6) WITH TIME ZONE;

CREATE INDEX idx_sessions_user_namespace ON sessions (user_id, namespace);
