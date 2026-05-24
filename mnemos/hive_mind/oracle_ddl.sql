-- mnemos/hive_mind/oracle_ddl.sql
-- Oracle 23ai DDL for the Hive Mind storage backend.
-- Contract source: mnemos/hive_mind/repository.py HiveMindRepository ABC.
-- Mirrors db/migrations_oracle/0010_hive_mind.sql using memory_ names.

CREATE TABLE memory_agents (
  urn                    VARCHAR2(256) PRIMARY KEY,
  kind                   VARCHAR2(64) NOT NULL,
  runtime                VARCHAR2(64),
  model                  VARCHAR2(128),
  provider               VARCHAR2(64),
  cost_tier              VARCHAR2(1) CHECK (cost_tier IN ('A','B','C')),
  autonomy_level         VARCHAR2(32) CHECK (autonomy_level IN ('autonomous','confirm-risky','interactive','unknown')),
  auth_method            VARCHAR2(32) DEFAULT 'unknown' NOT NULL CHECK (auth_method IN ('subscription','api','free','unknown')),
  plan_cap_usd           NUMBER(12,4) DEFAULT 50 NOT NULL,
  plan_period_used_usd   NUMBER(12,6) DEFAULT 0 NOT NULL,
  host                   VARCHAR2(128) NOT NULL,
  session_id             VARCHAR2(64) NOT NULL,
  pid                    NUMBER(10),
  capabilities           CLOB CHECK (capabilities IS JSON),
  version                VARCHAR2(64),
  started_at             TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  last_heartbeat         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  status                 VARCHAR2(16) NOT NULL CHECK (status IN ('online','idle','offline','error')),
  metadata               CLOB CHECK (metadata IS JSON)
);
CREATE INDEX idx_memory_agents_status    ON memory_agents(status);
CREATE INDEX idx_memory_agents_kind      ON memory_agents(kind);
CREATE INDEX idx_memory_agents_runtime   ON memory_agents(runtime);
CREATE INDEX idx_memory_agents_cost_tier ON memory_agents(cost_tier);
CREATE INDEX idx_memory_agents_host_pid  ON memory_agents(host, pid, status);

CREATE TABLE memory_jobs (
  id                    RAW(16) PRIMARY KEY,
  submitter_urn         VARCHAR2(256) NOT NULL,
  parent_job_id         RAW(16),
  kind                  VARCHAR2(64) NOT NULL,
  description           VARCHAR2(4000),
  priority              NUMBER(5) DEFAULT 0 NOT NULL,
  deadline              TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  required_capabilities CLOB CHECK (required_capabilities IS JSON),
  eligible_kinds        CLOB CHECK (eligible_kinds IS JSON),
  project               VARCHAR2(256),
  max_cost_tier         VARCHAR2(1) DEFAULT 'A' NOT NULL CHECK (max_cost_tier IN ('A','B','C')),
  preferred_providers   CLOB CHECK (preferred_providers IS JSON),
  preferred_models      CLOB CHECK (preferred_models IS JSON),
  mnemos_refs           CLOB CHECK (mnemos_refs IS JSON),
  depends_on            CLOB CHECK (depends_on IS JSON),
  max_retries           NUMBER(5) DEFAULT 2 NOT NULL,
  retry_count           NUMBER(5) DEFAULT 0 NOT NULL,
  retry_backoff_until   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  status                VARCHAR2(16) NOT NULL CHECK (status IN ('queued','offered','claimed','running','done','failed','cancelled')),
  claimed_by            VARCHAR2(256),
  claimed_at            TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  claimed_runtime       VARCHAR2(64),
  claimed_model         VARCHAR2(128),
  claimed_provider      VARCHAR2(64),
  claimed_cost_tier     VARCHAR2(1) CHECK (claimed_cost_tier IN ('A','B','C')),
  started_at            TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  ended_at              TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  result                CLOB CHECK (result IS JSON),
  result_mnemos_id      VARCHAR2(64),
  tokens_in             NUMBER(15),
  tokens_out            NUMBER(15),
  estimated_cost_usd    NUMBER(12,6)
);
CREATE INDEX idx_memory_jobs_status    ON memory_jobs(status);
CREATE INDEX memory_jobs_queue         ON memory_jobs(status, priority DESC, started_at ASC);
CREATE INDEX idx_memory_jobs_submitter ON memory_jobs(submitter_urn);
CREATE INDEX idx_memory_jobs_claimed   ON memory_jobs(claimed_by);
CREATE INDEX idx_memory_jobs_parent    ON memory_jobs(parent_job_id);
CREATE INDEX idx_memory_jobs_tier      ON memory_jobs(claimed_cost_tier);
CREATE INDEX idx_memory_jobs_project   ON memory_jobs(project);
CREATE INDEX idx_memory_jobs_retry     ON memory_jobs(status, retry_backoff_until);
CREATE INDEX idx_memory_jobs_costs     ON memory_jobs(ended_at, claimed_provider, claimed_model);

CREATE TABLE memory_messages (
  id           VARCHAR2(64) PRIMARY KEY,
  from_urn     VARCHAR2(256) NOT NULL,
  to_urn       VARCHAR2(256),
  in_reply_to  VARCHAR2(64),
  topic        VARCHAR2(256) NOT NULL,
  payload      CLOB NOT NULL CHECK (payload IS JSON),
  ts           TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);
CREATE INDEX idx_memory_messages_to    ON memory_messages(to_urn);
CREATE INDEX idx_memory_messages_topic ON memory_messages(topic);
CREATE INDEX idx_memory_messages_ts    ON memory_messages(ts);

CREATE SEQUENCE memory_events_seq START WITH 1 INCREMENT BY 1 NOCACHE;
CREATE TABLE memory_events (
  id          NUMBER(19) DEFAULT memory_events_seq.NEXTVAL PRIMARY KEY,
  ts          TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  kind        VARCHAR2(64) NOT NULL,
  payload     CLOB NOT NULL CHECK (payload IS JSON),
  agent_urn   VARCHAR2(256)
);
CREATE INDEX idx_memory_events_ts    ON memory_events(ts);
CREATE INDEX idx_memory_events_kind  ON memory_events(kind);
CREATE INDEX idx_memory_events_agent ON memory_events(agent_urn);

CREATE TABLE memory_hive_cache (
  cache_key         VARCHAR2(64) PRIMARY KEY,
  result_json       CLOB CHECK (result_json IS JSON),
  source_job_id     RAW(16) NOT NULL,
  result_mnemos_id  VARCHAR2(64),
  hit_count         NUMBER(15) DEFAULT 0 NOT NULL,
  cost_saved_usd    NUMBER(12,6) DEFAULT 0 NOT NULL,
  model             VARCHAR2(128),
  provider          VARCHAR2(64),
  cached_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  last_hit_at       TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_memory_hive_cache_cached ON memory_hive_cache(cached_at);
CREATE INDEX idx_memory_hive_cache_hits   ON memory_hive_cache(hit_count DESC, cost_saved_usd DESC);
CREATE INDEX idx_memory_hive_cache_source ON memory_hive_cache(source_job_id);

CREATE TABLE memory_worker_kind_stats (
  urn                 VARCHAR2(256) NOT NULL,
  kind                VARCHAR2(64) NOT NULL,
  success_count       NUMBER(15) DEFAULT 0 NOT NULL,
  fail_count          NUMBER(15) DEFAULT 0 NOT NULL,
  cancelled_count     NUMBER(15) DEFAULT 0 NOT NULL,
  total_tokens_in     NUMBER(15) DEFAULT 0 NOT NULL,
  total_tokens_out    NUMBER(15) DEFAULT 0 NOT NULL,
  total_cost_usd      NUMBER(12,6) DEFAULT 0 NOT NULL,
  total_duration_sec  NUMBER(15,3) DEFAULT 0 NOT NULL,
  last_run            TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT pk_memory_worker_kind_stats PRIMARY KEY (urn, kind)
);
CREATE INDEX idx_memory_wks_kind     ON memory_worker_kind_stats(kind);
CREATE INDEX idx_memory_wks_success  ON memory_worker_kind_stats(success_count DESC);
CREATE INDEX idx_memory_wks_last_run ON memory_worker_kind_stats(last_run);

CREATE TABLE memory_scheduled_jobs (
  id                VARCHAR2(64) PRIMARY KEY,
  name              VARCHAR2(256) NOT NULL,
  created_by_urn    VARCHAR2(256) NOT NULL,
  interval_seconds  NUMBER(10) NOT NULL,
  job_template      CLOB NOT NULL CHECK (job_template IS JSON),
  enabled           NUMBER(1) DEFAULT 1 NOT NULL CHECK (enabled IN (0,1)),
  last_fired_at     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  next_fire_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
  fire_count        NUMBER(15) DEFAULT 0 NOT NULL,
  created_at        TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);
CREATE INDEX idx_memory_sched_due     ON memory_scheduled_jobs(enabled, next_fire_at);
CREATE INDEX idx_memory_sched_creator ON memory_scheduled_jobs(created_by_urn);
CREATE INDEX idx_memory_sched_created ON memory_scheduled_jobs(created_at);
