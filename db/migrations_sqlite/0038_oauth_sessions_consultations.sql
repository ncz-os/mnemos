-- 0038_oauth_sessions_consultations.sql
-- SQLite parity tables for persistence-protocol OAuth/sessions/consultations.

CREATE TABLE IF NOT EXISTS oauth_tokens (
  token BLOB PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  scopes TEXT NOT NULL DEFAULT '[]',
  expires_at TEXT,
  refresh_token TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS oauth_state (
  state BLOB PRIMARY KEY,
  provider TEXT NOT NULL,
  csrf_token TEXT NOT NULL,
  return_url TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT NOT NULL
);

ALTER TABLE sessions ADD COLUMN session_id BLOB;
ALTER TABLE sessions ADD COLUMN started_at TEXT;
ALTER TABLE sessions ADD COLUMN last_active_at TEXT;
ALTER TABLE sessions ADD COLUMN metadata TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_session_id ON sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_session_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS session_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id BLOB NOT NULL,
  event_kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_session_logs_session_ts ON session_logs(session_id, ts);

CREATE TABLE IF NOT EXISTS consultations (
  id BLOB PRIMARY KEY,
  user_id TEXT NOT NULL,
  prompt TEXT NOT NULL,
  task_type TEXT,
  mode TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_consultations_user_created ON consultations(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_consultations_status_created ON consultations(status, created_at DESC);

CREATE TABLE IF NOT EXISTS consultation_responses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  consultation_id BLOB NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  response TEXT NOT NULL,
  final_score REAL,
  tokens_in INTEGER,
  tokens_out INTEGER,
  latency_ms INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_consultation_responses_consultation
  ON consultation_responses(consultation_id, id);
