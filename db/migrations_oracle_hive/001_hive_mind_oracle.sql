/*
  Oracle Hive Mind job-store schema.

  Contract source: mnemos/hive_mind/repository.py HiveMindRepository ABC.
  Job spec source: spec/p10 hive job 019e573d-e914.

  Scope note: this migration intentionally creates the four memory_* tables
  used by the hive job spec. It is not the broader seven-table hive_ schema in
  db/migrations_oracle/0010_hive_mind.sql.
*/

CREATE TABLE memory_jobs (
  id RAW(16) PRIMARY KEY,
  status VARCHAR2(20) NOT NULL,
  priority NUMBER(5) DEFAULT 0,
  kind VARCHAR2(100) NOT NULL,
  description CLOB,
  submitter_urn VARCHAR2(200) NOT NULL,
  claimed_by VARCHAR2(200),
  parent_job_id RAW(16),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
  started_at TIMESTAMP,
  ended_at TIMESTAMP,
  result CLOB CHECK (result IS JSON),
  eligible_kinds CLOB CHECK (eligible_kinds IS JSON),
  project VARCHAR2(100),
  tags CLOB CHECK (tags IS JSON),
  retry_count NUMBER(3) DEFAULT 0,
  max_retries NUMBER(3) DEFAULT 3
);

CREATE INDEX memory_jobs_queue ON memory_jobs(status, priority DESC, created_at ASC);
CREATE INDEX memory_jobs_parent ON memory_jobs(parent_job_id) WHERE parent_job_id IS NOT NULL;
CREATE INDEX memory_jobs_submitter ON memory_jobs(submitter_urn);
CREATE INDEX memory_jobs_claimed_by ON memory_jobs(claimed_by);
CREATE INDEX memory_jobs_project ON memory_jobs(project);

CREATE TABLE memory_agents (
  agent_urn VARCHAR2(200) PRIMARY KEY,
  kind VARCHAR2(50) NOT NULL,
  host VARCHAR2(100),
  registered_at TIMESTAMP DEFAULT SYSTIMESTAMP,
  last_heartbeat TIMESTAMP,
  capabilities CLOB CHECK (capabilities IS JSON)
);

CREATE INDEX memory_agents_kind_heartbeat ON memory_agents(kind, last_heartbeat);

CREATE TABLE memory_worker_kind_stats (
  kind VARCHAR2(50) PRIMARY KEY,
  success_count NUMBER DEFAULT 0,
  fail_count NUMBER DEFAULT 0,
  cancelled_count NUMBER DEFAULT 0,
  total_tokens_in NUMBER DEFAULT 0,
  total_tokens_out NUMBER DEFAULT 0,
  total_cost_usd NUMBER(12,6) DEFAULT 0,
  total_duration_sec NUMBER(12,3) DEFAULT 0,
  last_run TIMESTAMP
);

CREATE TABLE memory_hive_cache (
  cache_key VARCHAR2(500) PRIMARY KEY,
  value CLOB,
  expires_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP
);

CREATE INDEX memory_hive_cache_expires ON memory_hive_cache(expires_at);
