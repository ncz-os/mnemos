-- 0043_chat_sessions_parity_cols.sql — chat-session columns on the GRAEAE/chat
-- session tables (cross-backend parity). The SessionsRepository ABC is the CHAT
-- session interface (create_session(model), add_message, fetch_history,
-- memory-injections) — canonically backed by sessions / session_messages /
-- session_memory_injections (postgres migrations_v2_sessions + v3.5 namespace).
-- Oracle's sessions table was bootstrapped with an AUTH-ish shape (session_id RAW,
-- started_at, expires_at) and the chat columns were never added, so
-- OracleSessionsRepository could not be implemented. These ALTERs add the missing
-- chat columns additively — the auth columns + the auth create_session path
-- (OracleBackend.create_session) are untouched, so an auth-session row keeps a NULL
-- namespace and a chat-session row keeps a NULL session_id; the two are separable
-- by every query's WHERE clause (chat filters namespace, auth filters session_id).

ALTER TABLE sessions ADD (
  namespace      VARCHAR2(255),  -- nullable: auth-session rows keep NULL namespace (separable from chat),
  model          VARCHAR2(255),
  message_count  NUMBER DEFAULT 0 NOT NULL,
  total_tokens   NUMBER DEFAULT 0 NOT NULL,
  last_activity  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  deleted_at     TIMESTAMP WITH TIME ZONE
);

ALTER TABLE session_messages ADD (
  model             VARCHAR2(255),
  tokens_used       NUMBER,
  memories_injected NUMBER DEFAULT 0,
  deleted_at        TIMESTAMP WITH TIME ZONE
);

ALTER TABLE session_memory_injections ADD (
  message_id      NUMBER,
  relevance_score NUMBER,
  deleted_at      TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_sessions_user_ns ON sessions (user_id, namespace);
