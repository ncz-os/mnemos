-- migration: 0038_oauth_sessions_consultations
-- PostgreSQL mirror of Oracle 0038_oauth_sessions_consultations.

CREATE TABLE IF NOT EXISTS oauth_tokens (
  token BYTEA PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  scopes JSONB,
  expires_at TIMESTAMPTZ,
  refresh_token TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS oauth_state (
  state BYTEA PRIMARY KEY,
  provider TEXT NOT NULL,
  csrf_token TEXT NOT NULL,
  return_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS session_id BYTEA;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS metadata JSONB;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_session_id ON sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_session_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS session_logs (
  id BIGSERIAL PRIMARY KEY,
  session_id BYTEA NOT NULL,
  event_kind TEXT NOT NULL,
  payload JSONB,
  ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_logs_session_ts ON session_logs(session_id, ts);

CREATE TABLE IF NOT EXISTS consultations (
  id BYTEA PRIMARY KEY,
  user_id TEXT NOT NULL,
  prompt TEXT NOT NULL,
  task_type TEXT,
  mode TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_consultations_user_created ON consultations(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_consultations_status_created ON consultations(status, created_at DESC);

CREATE TABLE IF NOT EXISTS consultation_responses (
  id BIGSERIAL PRIMARY KEY,
  consultation_id BYTEA NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  response TEXT NOT NULL,
  final_score DOUBLE PRECISION,
  tokens_in INTEGER,
  tokens_out INTEGER,
  latency_ms INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_consultation_responses_consultation
  ON consultation_responses(consultation_id, id);
