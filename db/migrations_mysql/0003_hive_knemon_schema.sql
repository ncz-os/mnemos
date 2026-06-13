-- MNEMOS MySQL 8.0 Hive, audit-chain, usage-ledger, NATS/MCP/Pantheon, and
-- KNEMON schema tables mirroring the canonical PostgreSQL migrations.

CREATE TABLE IF NOT EXISTS hive_agents (
  urn                   VARCHAR(256) NOT NULL,
  kind                  VARCHAR(64) NOT NULL,
  host                  VARCHAR(128) NOT NULL,
  session_id            VARCHAR(128) NOT NULL,
  pid                   INT,
  capabilities          JSON,
  version               VARCHAR(64),
  started_at            DOUBLE NOT NULL,
  last_heartbeat        DOUBLE NOT NULL,
  status                VARCHAR(16) NOT NULL,
  metadata              JSON,
  runtime               VARCHAR(64),
  model                 VARCHAR(128),
  provider              VARCHAR(64),
  autonomy_level        VARCHAR(32),
  cost_tier             VARCHAR(2),
  current_load          VARCHAR(32),
  auth_method           VARCHAR(64),
  plan_cap_usd          DECIMAL(12,4),
  plan_period_used_usd  DECIMAL(12,4) DEFAULT 0,
  subscription_pools    JSON,
  PRIMARY KEY (urn),
  KEY idx_hive_agents_kind (kind),
  KEY idx_hive_agents_status (status),
  CHECK (status IN ('online','idle','offline','error'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hive_jobs (
  id                       VARCHAR(64) NOT NULL,
  submitter_urn            VARCHAR(256) NOT NULL,
  parent_job_id            VARCHAR(64),
  kind                     VARCHAR(256) NOT NULL,
  title                    VARCHAR(512),
  description              TEXT,
  priority                 INT NOT NULL DEFAULT 0,
  deadline                 DOUBLE,
  required_capabilities    JSON,
  eligible_kinds           JSON,
  status                   VARCHAR(16) NOT NULL,
  claimed_by               VARCHAR(256),
  claimed_at               DOUBLE,
  started_at               DOUBLE NOT NULL,
  ended_at                 DOUBLE,
  result                   JSON,
  required_autonomy        VARCHAR(32),
  max_cost_tier            VARCHAR(2),
  preferred_providers      JSON,
  preferred_models         JSON,
  claimed_runtime          VARCHAR(64),
  claimed_model            VARCHAR(128),
  claimed_provider         VARCHAR(64),
  claimed_cost_tier        VARCHAR(2),
  tokens_in                BIGINT,
  tokens_out               BIGINT,
  estimated_cost_usd       DECIMAL(12,6),
  mnemos_refs              JSON,
  result_mnemos_id         VARCHAR(64),
  required_resources       JSON,
  claimed_host_caps        JSON,
  project                  VARCHAR(64),
  tags                     JSON,
  depends_on               JSON,
  retry_count              INT NOT NULL DEFAULT 0,
  max_retries              INT NOT NULL DEFAULT 2,
  retry_backoff_until      DOUBLE,
  last_update_at           DOUBLE,
  PRIMARY KEY (id),
  KEY idx_hive_jobs_status_priority (status, priority),
  KEY idx_hive_jobs_claimed_by (claimed_by),
  KEY idx_hive_jobs_project (project),
  CHECK (status IN ('queued','offered','claimed','running','done','failed','cancelled'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hive_messages (
  id           VARCHAR(64) NOT NULL,
  from_urn     VARCHAR(256) NOT NULL,
  to_urn       VARCHAR(256),
  in_reply_to  VARCHAR(64),
  topic        VARCHAR(128) NOT NULL,
  payload      JSON NOT NULL,
  ts           DOUBLE NOT NULL,
  PRIMARY KEY (id),
  KEY idx_hive_messages_to_ts (to_urn, ts),
  KEY idx_hive_messages_topic_ts (topic, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hive_events (
  id         BIGINT NOT NULL AUTO_INCREMENT,
  ts         DOUBLE NOT NULL,
  kind       VARCHAR(64) NOT NULL,
  payload    JSON NOT NULL,
  agent_urn  VARCHAR(256),
  PRIMARY KEY (id),
  KEY idx_hive_events_ts (ts),
  KEY idx_hive_events_kind_ts (kind, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hive_cache (
  cache_key        VARCHAR(128) NOT NULL,
  result_json      JSON NOT NULL,
  source_job_id    VARCHAR(64),
  result_mnemos_id VARCHAR(64),
  hit_count        BIGINT NOT NULL DEFAULT 0,
  cost_saved_usd   DECIMAL(12,6) NOT NULL DEFAULT 0,
  model            VARCHAR(128),
  provider         VARCHAR(64),
  cached_at        DOUBLE NOT NULL,
  last_hit_at      DOUBLE,
  PRIMARY KEY (cache_key),
  KEY idx_hive_cache_source_job (source_job_id),
  KEY idx_hive_cache_model_provider (model, provider)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hive_worker_kind_stats (
  urn                 VARCHAR(256) NOT NULL,
  kind                VARCHAR(256) NOT NULL,
  success_count       BIGINT NOT NULL DEFAULT 0,
  fail_count          BIGINT NOT NULL DEFAULT 0,
  cancelled_count     BIGINT NOT NULL DEFAULT 0,
  total_tokens_in     BIGINT NOT NULL DEFAULT 0,
  total_tokens_out    BIGINT NOT NULL DEFAULT 0,
  total_cost_usd      DECIMAL(15,6) NOT NULL DEFAULT 0,
  total_duration_sec  DECIMAL(15,3) NOT NULL DEFAULT 0,
  last_run            DOUBLE,
  PRIMARY KEY (urn, kind),
  KEY idx_hive_wkstats_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hive_scheduled_jobs (
  id                VARCHAR(64) NOT NULL,
  name              VARCHAR(256) NOT NULL,
  created_by_urn    VARCHAR(256) NOT NULL,
  interval_seconds  BIGINT NOT NULL,
  job_template      JSON NOT NULL,
  enabled           SMALLINT NOT NULL DEFAULT 1,
  last_fired_at     DOUBLE,
  next_fire_at      DOUBLE NOT NULL,
  fire_count        BIGINT NOT NULL DEFAULT 0,
  created_at        DOUBLE NOT NULL,
  PRIMARY KEY (id),
  KEY idx_hive_scheduled_next (enabled, next_fire_at),
  CHECK (enabled IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS memory_audit_chain (
  entry_id         VARBINARY(16) NOT NULL,
  memory_id        VARBINARY(16) NOT NULL,
  prev_entry_id    VARBINARY(16),
  prev_entry_hash  VARBINARY(32),
  op               VARCHAR(16) NOT NULL,
  payload_hash     VARBINARY(32) NOT NULL,
  writer_id        VARCHAR(128) NOT NULL,
  writer_pubkey    VARBINARY(32) NOT NULL,
  signature        VARBINARY(64) NOT NULL,
  signed_at        DATETIME(6) NOT NULL,
  global_root      VARBINARY(32),
  global_seq       BIGINT,
  PRIMARY KEY (entry_id),
  KEY idx_memory_audit_chain_memory (memory_id),
  KEY idx_memory_audit_chain_global_seq (global_seq),
  CHECK (op IN ('create','update','delete','archive','replicate'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS memory_audit_roots (
  global_root     VARBINARY(32) NOT NULL,
  window_start    DATETIME(6) NOT NULL,
  window_end      DATETIME(6) NOT NULL,
  entry_count     BIGINT NOT NULL,
  root_signature  VARBINARY(64) NOT NULL,
  signer_pubkey   VARBINARY(32) NOT NULL,
  sealed_at       DATETIME(6) NOT NULL,
  PRIMARY KEY (global_root),
  KEY idx_memory_audit_roots_window (window_start, window_end),
  CHECK (window_end > window_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS memory_category_decay (
  category        VARCHAR(64) NOT NULL,
  half_life_days  DECIMAL(10,2) NOT NULL,
  decay_kind      VARCHAR(16) NOT NULL,
  floor           DECIMAL(5,4) NOT NULL DEFAULT 0,
  PRIMARY KEY (category),
  CHECK (decay_kind IN ('exponential','sigmoid','none')),
  CHECK (floor >= 0 AND floor <= 1),
  CHECK (half_life_days > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS usage_ledger (
  id                         BIGINT PRIMARY KEY AUTO_INCREMENT,
  provider                   TEXT NOT NULL,
  model                      TEXT NOT NULL,
  task_kind                  TEXT NOT NULL,
  tokens_in                  INT NOT NULL,
  tokens_out                 INT NOT NULL,
  tokens_reasoning           INT NOT NULL DEFAULT 0,
  est_cost_usd               DECIMAL(12,6) NOT NULL,
  latency_ms                 INT NOT NULL,
  outcome                    TEXT NOT NULL,
  caller_subsystem           TEXT NOT NULL,
  tier                       TEXT NOT NULL,
  ts                         DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  session_id                 TEXT,
  request_count              DECIMAL(20,6) NOT NULL DEFAULT 1,
  plan_window_id             TEXT,
  subscription_amortized     TINYINT(1) NOT NULL DEFAULT 0,
  path_kind                  TEXT NOT NULL,
  gateway_provider           TEXT,
  gateway_model              TEXT,
  cost                       DECIMAL(12,6),
  KEY usage_ledger_ts_idx (ts),
  KEY usage_ledger_session_idx (session_id(191)),
  KEY usage_ledger_window_idx (plan_window_id(191)),
  CHECK (tokens_in >= 0),
  CHECK (tokens_out >= 0),
  CHECK (tokens_reasoning >= 0),
  CHECK (est_cost_usd >= 0),
  CHECK (latency_ms >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS subscription_plans (
  provider                    VARCHAR(255) NOT NULL,
  plan_name                   VARCHAR(255) NOT NULL,
  auth_method                 TEXT NOT NULL,
  monthly_usd                 DECIMAL(12,2),
  msg_cap                     DECIMAL(20,6),
  msg_window_seconds          DECIMAL(20,6),
  token_cap                   DECIMAL(20,6),
  token_window_seconds        DECIMAL(20,6),
  reset_anchor                TEXT,
  overage_pricing_per_mtok_in DECIMAL(20,6),
  overage_pricing_per_mtok_out DECIMAL(20,6),
  notes                       TEXT,
  effective_from              DATE NOT NULL DEFAULT '2026-01-01',
  effective_until             DATE,
  path_kind                   TEXT NOT NULL,
  parent_plan_id              TEXT,
  PRIMARY KEY (provider, plan_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS nats_dispatch_log (
  event_id       VARCHAR(255) NOT NULL,
  subject        VARCHAR(255) NOT NULL,
  dispatched_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (event_id, subject),
  KEY idx_nats_dispatch_at (dispatched_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS mcp_audit_log (
  id               CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  caller_user_id   TEXT NOT NULL,
  role             TEXT NOT NULL,
  tool             TEXT NOT NULL,
  parameter_shape  JSON NOT NULL DEFAULT (JSON_OBJECT()),
  outcome          TEXT NOT NULL,
  error_class      TEXT,
  created_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY idx_mcp_audit_user_created (caller_user_id(191), created_at),
  KEY idx_mcp_audit_tool_created (tool(191), created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS pantheon_routing_audit (
  id              CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  request_id      TEXT,
  tenant_user_id  TEXT,
  alias_or_model  TEXT,
  resolved_to     TEXT,
  outcome         TEXT,
  latency_ms      INT,
  tokens_in       INT,
  tokens_out      INT,
  cost_usd        DECIMAL(10,4),
  error_class     TEXT,
  payload         JSON NOT NULL,
  created         DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
  KEY idx_pantheon_routing_created (created),
  KEY idx_pantheon_routing_tenant (tenant_user_id(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS memory_acl (
  memory_id   VARCHAR(255) NOT NULL,
  principal   VARCHAR(512) NOT NULL,
  perm        SMALLINT NOT NULL DEFAULT 4,
  granted_by  TEXT,
  created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (memory_id, principal),
  KEY idx_memory_acl_principal (principal),
  CHECK (perm >= 0 AND perm <= 7),
  CONSTRAINT fk_memory_acl_memory FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knemon_tier_assignments (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  task_kind       VARCHAR(256) NOT NULL,
  tier            VARCHAR(4) NOT NULL,
  events_total    BIGINT NOT NULL,
  sessions_total  INT NOT NULL,
  events_per_day  DECIMAL(9,3) NOT NULL,
  p95_latency_ms  BIGINT NOT NULL,
  avg_latency_ms  BIGINT NOT NULL,
  iteration       INT NOT NULL DEFAULT 0,
  last_updated    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  UNIQUE KEY uq_knemon_tier_task_kind (task_kind),
  KEY ix_knemon_tier_tier (tier),
  KEY ix_knemon_tier_iter (iteration),
  CHECK (tier IN ('B1','B2','C1','C2'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knemon_phase1_baseline_2026_05_28 (
  event_id        BIGINT NOT NULL,
  session_urn     VARCHAR(256),
  plan_window_id  VARCHAR(128),
  task_kind       VARCHAR(128) NOT NULL,
  provider        VARCHAR(128) NOT NULL,
  model           VARCHAR(256) NOT NULL,
  tokens_in       BIGINT NOT NULL,
  tokens_out      BIGINT NOT NULL,
  cost_usd        DECIMAL(14,8) NOT NULL,
  ts_utc          DATETIME(6) NOT NULL,
  PRIMARY KEY (event_id),
  KEY ix_knemon_p1b_task_kind (task_kind),
  KEY ix_knemon_p1b_provider (provider),
  KEY ix_knemon_p1b_model (provider, model),
  KEY ix_knemon_p1b_session (session_urn),
  KEY ix_knemon_p1b_ts (ts_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knemon_baselines (
  id               BIGINT PRIMARY KEY AUTO_INCREMENT,
  baseline_name    VARCHAR(128) NOT NULL,
  table_name       VARCHAR(128) NOT NULL,
  window_start     DATETIME(6) NOT NULL,
  window_end       DATETIME(6) NOT NULL,
  event_count      BIGINT NOT NULL,
  session_count    INT NOT NULL,
  task_kind_count  INT NOT NULL,
  source_table     VARCHAR(128) NOT NULL,
  created_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  notes            TEXT,
  UNIQUE KEY uq_knemon_baseline_name (baseline_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
