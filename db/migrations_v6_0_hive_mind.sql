-- migrations_v6_0_hive_mind.sql
-- GRAEAE Hive Mind schema for PostgreSQL (default mnemos `server` profile).
-- Adds agent coordination + triage queue tables alongside existing memory schema.
-- Apply order: after migrations_v5_3_5_model_registry_capabilities_gin.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS hive_agents (
  urn              TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,
  runtime          TEXT,
  model            TEXT,
  provider         TEXT,
  cost_tier        TEXT CHECK (cost_tier IN ('A','B','C')),
  autonomy_level   TEXT CHECK (autonomy_level IN ('autonomous','confirm-risky','interactive','unknown')),
  host             TEXT NOT NULL,
  session_id       TEXT NOT NULL,
  pid              INTEGER,
  capabilities     JSONB,
  version          TEXT,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_heartbeat   TIMESTAMPTZ NOT NULL DEFAULT now(),
  status           TEXT NOT NULL CHECK (status IN ('online','idle','offline','error')),
  metadata         JSONB
);
CREATE INDEX IF NOT EXISTS idx_hive_agents_status  ON hive_agents(status);
CREATE INDEX IF NOT EXISTS idx_hive_agents_kind    ON hive_agents(kind);
CREATE INDEX IF NOT EXISTS idx_hive_agents_runtime ON hive_agents(runtime);
CREATE INDEX IF NOT EXISTS idx_hive_agents_tier    ON hive_agents(cost_tier);

CREATE TABLE IF NOT EXISTS hive_jobs (
  id                   TEXT PRIMARY KEY,
  submitter_urn        TEXT NOT NULL,
  parent_job_id        TEXT,
  kind                 TEXT NOT NULL,
  description          TEXT,
  priority             INTEGER NOT NULL DEFAULT 0,
  deadline             TIMESTAMPTZ,
  required_capabilities JSONB,
  eligible_kinds       JSONB,
  max_cost_tier        TEXT NOT NULL DEFAULT 'A' CHECK (max_cost_tier IN ('A','B','C')),
  preferred_providers  JSONB,
  preferred_models     JSONB,
  mnemos_refs          JSONB,
  status               TEXT NOT NULL CHECK (status IN ('queued','offered','claimed','running','done','failed','cancelled')),
  claimed_by           TEXT,
  claimed_at           TIMESTAMPTZ,
  claimed_runtime      TEXT,
  claimed_model        TEXT,
  claimed_provider     TEXT,
  claimed_cost_tier    TEXT,
  started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at             TIMESTAMPTZ,
  result               JSONB,
  result_mnemos_id     TEXT,
  tokens_in            BIGINT,
  tokens_out           BIGINT,
  tokens_reasoning     BIGINT,
  provider             TEXT,
  model                TEXT,
  cost_usd_est         NUMERIC(12,6),
  estimated_cost_usd   NUMERIC(12,6)
);
CREATE INDEX IF NOT EXISTS idx_hive_jobs_status    ON hive_jobs(status);
CREATE INDEX IF NOT EXISTS idx_hive_jobs_queue     ON hive_jobs(status, priority DESC, started_at ASC);
CREATE INDEX IF NOT EXISTS idx_hive_jobs_submitter ON hive_jobs(submitter_urn);
CREATE INDEX IF NOT EXISTS idx_hive_jobs_claimed   ON hive_jobs(claimed_by);
CREATE INDEX IF NOT EXISTS idx_hive_jobs_parent    ON hive_jobs(parent_job_id);
CREATE INDEX IF NOT EXISTS idx_hive_jobs_tier      ON hive_jobs(claimed_cost_tier);

ALTER TABLE hive_jobs ADD COLUMN IF NOT EXISTS tokens_in BIGINT;
ALTER TABLE hive_jobs ADD COLUMN IF NOT EXISTS tokens_out BIGINT;
ALTER TABLE hive_jobs ADD COLUMN IF NOT EXISTS tokens_reasoning BIGINT;
ALTER TABLE hive_jobs ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE hive_jobs ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE hive_jobs ADD COLUMN IF NOT EXISTS cost_usd_est NUMERIC(12,6);

CREATE TABLE IF NOT EXISTS hive_messages (
  id           TEXT PRIMARY KEY,
  from_urn     TEXT NOT NULL,
  to_urn       TEXT,
  in_reply_to  TEXT,
  topic        TEXT NOT NULL,
  payload      JSONB NOT NULL,
  ts           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_hive_messages_to    ON hive_messages(to_urn);
CREATE INDEX IF NOT EXISTS idx_hive_messages_topic ON hive_messages(topic);
CREATE INDEX IF NOT EXISTS idx_hive_messages_ts    ON hive_messages(ts);

CREATE TABLE IF NOT EXISTS hive_events (
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind        TEXT NOT NULL,
  payload     JSONB NOT NULL,
  agent_urn   TEXT
);
CREATE INDEX IF NOT EXISTS idx_hive_events_ts    ON hive_events(ts);
CREATE INDEX IF NOT EXISTS idx_hive_events_kind  ON hive_events(kind);
CREATE INDEX IF NOT EXISTS idx_hive_events_agent ON hive_events(agent_urn);

-- LISTEN/NOTIFY trigger for SSE push (replaces polling in Phase 2)
CREATE OR REPLACE FUNCTION hive_event_notify() RETURNS TRIGGER AS $$
BEGIN
  PERFORM pg_notify('hive_events', json_build_object(
    'id', NEW.id, 'kind', NEW.kind, 'ts', NEW.ts, 'agent_urn', NEW.agent_urn
  )::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS hive_events_notify_trg ON hive_events;
CREATE TRIGGER hive_events_notify_trg AFTER INSERT ON hive_events
  FOR EACH ROW EXECUTE FUNCTION hive_event_notify();

COMMIT;
