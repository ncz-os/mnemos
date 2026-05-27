-- 0010_hive_mind.sql
-- GRAEAE Hive Mind schema for Oracle 23ai (PYTHIA production backend).
-- Uses Oracle native JSON columns + sequences. SKIP LOCKED-friendly via FOR UPDATE.

CREATE TABLE hive_agents (
  urn              VARCHAR2(256) PRIMARY KEY,
  kind             VARCHAR2(64) NOT NULL,
  runtime          VARCHAR2(64),
  model            VARCHAR2(128),
  provider         VARCHAR2(64),
  cost_tier        VARCHAR2(1) CHECK (cost_tier IN ('A','B','C')),
  autonomy_level   VARCHAR2(32) CHECK (autonomy_level IN ('autonomous','confirm-risky','interactive','unknown')),
  host             VARCHAR2(128) NOT NULL,
  session_id       VARCHAR2(64) NOT NULL,
  pid              NUMBER(10),
  capabilities     CLOB CHECK (capabilities IS JSON),
  version          VARCHAR2(64),
  started_at       TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  last_heartbeat   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  status           VARCHAR2(16) NOT NULL CHECK (status IN ('online','idle','offline','error')),
  metadata         CLOB CHECK (metadata IS JSON)
);
CREATE INDEX idx_hive_agents_status   ON hive_agents(status);
CREATE INDEX idx_hive_agents_kind     ON hive_agents(kind);
CREATE INDEX idx_hive_agents_runtime  ON hive_agents(runtime);
CREATE INDEX idx_hive_agents_cost_tier ON hive_agents(cost_tier);

CREATE TABLE hive_jobs (
  id                    VARCHAR2(64) PRIMARY KEY,
  submitter_urn         VARCHAR2(256) NOT NULL,
  parent_job_id         VARCHAR2(64),
  kind                  VARCHAR2(64) NOT NULL,
  description           VARCHAR2(4000),
  priority              NUMBER(5) DEFAULT 0 NOT NULL,
  deadline              TIMESTAMP WITH TIME ZONE,
  required_capabilities CLOB CHECK (required_capabilities IS JSON),
  eligible_kinds        CLOB CHECK (eligible_kinds IS JSON),
  max_cost_tier         VARCHAR2(1) DEFAULT 'A' NOT NULL CHECK (max_cost_tier IN ('A','B','C')),
  preferred_providers   CLOB CHECK (preferred_providers IS JSON),
  preferred_models      CLOB CHECK (preferred_models IS JSON),
  mnemos_refs           CLOB CHECK (mnemos_refs IS JSON),
  status                VARCHAR2(16) NOT NULL CHECK (status IN ('queued','offered','claimed','running','done','failed','cancelled')),
  claimed_by            VARCHAR2(256),
  claimed_at            TIMESTAMP WITH TIME ZONE,
  claimed_runtime       VARCHAR2(64),
  claimed_model         VARCHAR2(128),
  claimed_provider      VARCHAR2(64),
  claimed_cost_tier     VARCHAR2(1),
  started_at            TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  ended_at              TIMESTAMP WITH TIME ZONE,
  result                CLOB CHECK (result IS JSON),
  result_mnemos_id      VARCHAR2(64),
  tokens_in             NUMBER(15),
  tokens_out            NUMBER(15),
  tokens_reasoning      NUMBER(15),
  provider              VARCHAR2(64),
  model                 VARCHAR2(128),
  cost_usd_est          NUMBER(12,6),
  estimated_cost_usd    NUMBER(12,6)
);
CREATE INDEX idx_hive_jobs_status    ON hive_jobs(status);
CREATE INDEX idx_hive_jobs_queue     ON hive_jobs(status, priority DESC, started_at ASC);
CREATE INDEX idx_hive_jobs_submitter ON hive_jobs(submitter_urn);
CREATE INDEX idx_hive_jobs_claimed   ON hive_jobs(claimed_by);
CREATE INDEX idx_hive_jobs_parent    ON hive_jobs(parent_job_id);
CREATE INDEX idx_hive_jobs_tier      ON hive_jobs(claimed_cost_tier);

CREATE TABLE hive_messages (
  id           VARCHAR2(64) PRIMARY KEY,
  from_urn     VARCHAR2(256) NOT NULL,
  to_urn       VARCHAR2(256),
  in_reply_to  VARCHAR2(64),
  topic        VARCHAR2(256) NOT NULL,
  payload      CLOB NOT NULL CHECK (payload IS JSON),
  ts           TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);
CREATE INDEX idx_hive_messages_to    ON hive_messages(to_urn);
CREATE INDEX idx_hive_messages_topic ON hive_messages(topic);
CREATE INDEX idx_hive_messages_ts    ON hive_messages(ts);

CREATE SEQUENCE hive_events_seq START WITH 1 INCREMENT BY 1 NOCACHE;
CREATE TABLE hive_events (
  id          NUMBER(19) DEFAULT hive_events_seq.NEXTVAL PRIMARY KEY,
  ts          TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  kind        VARCHAR2(64) NOT NULL,
  payload     CLOB NOT NULL CHECK (payload IS JSON),
  agent_urn   VARCHAR2(256)
);
CREATE INDEX idx_hive_events_ts    ON hive_events(ts);
CREATE INDEX idx_hive_events_kind  ON hive_events(kind);
CREATE INDEX idx_hive_events_agent ON hive_events(agent_urn);
