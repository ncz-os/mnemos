-- 0041_chat_sessions_parity_cols.sql — chat-session columns on the Db2 chat
-- session tables (cross-backend parity; mirrors oracle 0043). The Db2 sessions
-- tables were bootstrapped with an AUTH shape; the SessionsRepository ABC is the
-- CHAT interface, so the chat columns were missing and Db2SessionsRepository
-- could not implement them. Added additively; namespace is nullable so any
-- (vestigial, unwired) auth-session row keeps a NULL namespace and stays
-- separable from chat rows (every chat query filters namespace).

ALTER TABLE sessions ADD COLUMN namespace VARCHAR(255);
ALTER TABLE sessions ADD COLUMN model VARCHAR(255);
ALTER TABLE sessions ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN last_activity TIMESTAMP NOT NULL DEFAULT CURRENT TIMESTAMP;
ALTER TABLE sessions ADD COLUMN deleted_at TIMESTAMP;

CALL SYSPROC.ADMIN_CMD('REORG TABLE sessions');

ALTER TABLE session_messages ADD COLUMN model VARCHAR(255);
ALTER TABLE session_messages ADD COLUMN tokens_used INTEGER;
ALTER TABLE session_messages ADD COLUMN memories_injected INTEGER DEFAULT 0;
ALTER TABLE session_messages ADD COLUMN deleted_at TIMESTAMP;

CALL SYSPROC.ADMIN_CMD('REORG TABLE session_messages');

ALTER TABLE session_memory_injections ADD COLUMN message_id BIGINT;
ALTER TABLE session_memory_injections ADD COLUMN relevance_score DOUBLE;
ALTER TABLE session_memory_injections ADD COLUMN deleted_at TIMESTAMP;

CALL SYSPROC.ADMIN_CMD('REORG TABLE session_memory_injections');

CREATE INDEX idx_sessions_user_ns ON sessions (user_id, namespace);
