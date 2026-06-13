-- MNEMOS MySQL 8.0 feature-parity schema: sessions, OAuth, webhooks,
-- model registry, federation, MORPHEUS, Hive, audit, usage, MCP/Pantheon,
-- GRAEAE response tables, and KNEMON operational tables.

CREATE TABLE IF NOT EXISTS sessions (
  id                VARCHAR(255) PRIMARY KEY DEFAULT (UUID()),
  user_id           VARCHAR(255) NOT NULL,
  created_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_activity     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at        DATETIME(6) NOT NULL,
  model             TEXT NOT NULL,
  compression_tier  INT NOT NULL DEFAULT 1,
  message_count     INT NOT NULL DEFAULT 0,
  total_tokens      INT NOT NULL DEFAULT 0,
  metadata          JSON,
  namespace         VARCHAR(255) NOT NULL DEFAULT 'default',
  session_id        VARBINARY(255),
  started_at        DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
  last_active_at    DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
  deleted_at        DATETIME(6),
  KEY idx_sessions_user_created (user_id, created_at),
  KEY idx_sessions_expires (expires_at),
  KEY idx_sessions_namespace (user_id, namespace),
  CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS session_messages (
  id                  VARCHAR(255) PRIMARY KEY DEFAULT (UUID()),
  session_id          VARCHAR(255) NOT NULL,
  message_id          VARCHAR(255) NOT NULL DEFAULT (UUID()),
  role                TEXT NOT NULL,
  content             LONGTEXT NOT NULL,
  `timestamp`         DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  model               TEXT,
  tokens_used         INT,
  memories_injected   INT DEFAULT 0,
  compression_ratio   DOUBLE,
  metadata            JSON,
  deleted_at          DATETIME(6),
  KEY idx_session_messages_session_ts (session_id, `timestamp`),
  CONSTRAINT fk_session_messages_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS session_memory_injections (
  id                    VARCHAR(255) PRIMARY KEY DEFAULT (UUID()),
  session_id            VARCHAR(255) NOT NULL,
  message_id            VARCHAR(255),
  memory_id             VARCHAR(255),
  relevance_score       DOUBLE,
  compressed            TINYINT(1) DEFAULT 1,
  compression_ratio     DOUBLE,
  injection_timestamp   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  injected_at           DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  deleted_at            DATETIME(6),
  KEY idx_smi_session (session_id),
  KEY idx_smi_memory (memory_id),
  CONSTRAINT fk_smi_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
  CONSTRAINT fk_smi_memory FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS session_logs (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  session_id  VARBINARY(255) NOT NULL,
  event_kind  TEXT NOT NULL,
  payload     JSON,
  ts          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY idx_session_logs_session (session_id),
  KEY idx_session_logs_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS graeae_consultations (
  id                       CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  prompt                   LONGTEXT NOT NULL,
  task_type                VARCHAR(255),
  context_uncompressed     LONGTEXT,
  context_compressed       LONGTEXT,
  context_quality_rating   INT,
  context_quality_summary  JSON,
  compression_manifest     JSON,
  context_memory_ids       JSON,
  consensus_response       LONGTEXT NOT NULL,
  consensus_score          DOUBLE,
  winning_muse             VARCHAR(100),
  cost                     DOUBLE,
  latency_ms               INT,
  mode                     VARCHAR(50),
  created                  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  model_variants           JSON,
  owner_id                 VARCHAR(255) NOT NULL DEFAULT 'default',
  namespace                VARCHAR(255) NOT NULL DEFAULT 'default',
  deleted_at               DATETIME(6),
  KEY idx_graeae_consult_created (created),
  KEY idx_graeae_consult_task_type (task_type),
  KEY idx_graeae_consult_owner_namespace (owner_id, namespace),
  CHECK (context_quality_rating IS NULL OR (context_quality_rating >= 0 AND context_quality_rating <= 100)),
  CHECK (consensus_score IS NULL OR (consensus_score >= 0 AND consensus_score <= 1.0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS graeae_audit_log (
  id                CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  sequence_num      BIGINT NOT NULL AUTO_INCREMENT UNIQUE,
  consultation_id   CHAR(36),
  prompt            LONGTEXT,
  prompt_hash       VARCHAR(64),
  response_hash     VARCHAR(64),
  chain_hash        VARCHAR(64),
  prev_id           CHAR(36),
  task_type         VARCHAR(100),
  provider          VARCHAR(50),
  quality_score     DOUBLE,
  created_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  response_text     LONGTEXT,
  prev_chain_hash   VARCHAR(64),
  model             VARCHAR(100),
  latency_ms        INT,
  cost_usd          DOUBLE,
  deleted_at        DATETIME(6),
  KEY idx_audit_sequence (sequence_num),
  KEY idx_audit_created (created_at),
  KEY idx_audit_consultation (consultation_id),
  KEY idx_audit_chain_hash (chain_hash),
  CONSTRAINT fk_audit_consultation FOREIGN KEY (consultation_id) REFERENCES graeae_consultations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS consultation_memory_refs (
  id              CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  consultation_id CHAR(36) NOT NULL,
  memory_id       VARCHAR(255),
  relevance_score DOUBLE,
  injected_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY unique_consultation_memory (consultation_id, memory_id),
  KEY idx_cmr_memory (memory_id),
  KEY idx_cmr_injected (injected_at),
  CONSTRAINT fk_cmr_consultation FOREIGN KEY (consultation_id) REFERENCES graeae_consultations(id) ON DELETE CASCADE,
  CONSTRAINT fk_cmr_memory FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS consultations (
  id            VARBINARY(255) PRIMARY KEY,
  user_id       VARCHAR(255) NOT NULL,
  prompt        LONGTEXT NOT NULL,
  task_type     TEXT,
  mode          TEXT,
  status        TEXT NOT NULL,
  created_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  completed_at  DATETIME(6),
  KEY idx_consultations_user (user_id),
  KEY idx_consultations_status (status(64))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS consultation_responses (
  id               BIGINT PRIMARY KEY AUTO_INCREMENT,
  consultation_id  VARBINARY(255) NOT NULL,
  provider         TEXT NOT NULL,
  model_id         TEXT NOT NULL,
  response         LONGTEXT NOT NULL,
  final_score      DOUBLE,
  tokens_in        INT,
  tokens_out       INT,
  latency_ms       INT,
  created_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY idx_consultation_responses_consultation (consultation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS model_registry (
  id                    CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  provider              VARCHAR(50) NOT NULL,
  model_id              VARCHAR(512) NOT NULL,
  display_name          TEXT,
  family                TEXT,
  context_window        INT,
  max_output_tokens     INT,
  capabilities          JSON DEFAULT (JSON_ARRAY()),
  input_cost_per_mtok   DECIMAL(12,6) DEFAULT 0,
  output_cost_per_mtok  DECIMAL(12,6) DEFAULT 0,
  cache_read_per_mtok   DECIMAL(12,6) DEFAULT 0,
  cache_write_per_mtok  DECIMAL(12,6) DEFAULT 0,
  available             TINYINT(1) NOT NULL DEFAULT 1,
  deprecated            TINYINT(1) NOT NULL DEFAULT 0,
  arena_score           DECIMAL(8,2),
  arena_rank            INT,
  graeae_weight         DECIMAL(5,4),
  first_seen            DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_seen             DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_synced           DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  raw                   JSON DEFAULT (JSON_OBJECT()),
  price_in              DECIMAL(12,6) DEFAULT 0,
  price_out             DECIMAL(12,6) DEFAULT 0,
  price_cached          DECIMAL(12,6) DEFAULT 0,
  price_updated_at      DATETIME(6),
  UNIQUE KEY uq_model_registry_provider_model (provider, model_id),
  KEY idx_model_registry_provider (provider),
  KEY idx_model_registry_available (available),
  KEY idx_model_registry_arena_score (arena_score),
  KEY idx_model_registry_graeae_weight (graeae_weight),
  KEY idx_model_registry_last_synced (last_synced)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS model_registry_sync_log (
  id                 CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  provider           VARCHAR(50) NOT NULL,
  synced_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  models_found       INT NOT NULL DEFAULT 0,
  models_added       INT NOT NULL DEFAULT 0,
  models_updated     INT NOT NULL DEFAULT 0,
  models_deprecated  INT NOT NULL DEFAULT 0,
  error              TEXT,
  duration_ms        INT,
  KEY idx_model_registry_sync_provider (provider),
  KEY idx_model_registry_sync_at (synced_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS price_history (
  id            CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  provider      VARCHAR(50) NOT NULL,
  model_id      VARCHAR(512) NOT NULL,
  price_in      DECIMAL(12,6),
  price_out     DECIMAL(12,6),
  price_cached  DECIMAL(12,6),
  prices        JSON DEFAULT (JSON_OBJECT()),
  recorded_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY idx_price_history_model (provider, model_id),
  KEY idx_price_history_recorded (recorded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS oauth_providers (
  name            VARCHAR(255) PRIMARY KEY,
  display_name    TEXT NOT NULL,
  kind            VARCHAR(32) NOT NULL DEFAULT 'oidc',
  issuer_url      TEXT,
  client_id       TEXT NOT NULL,
  client_secret   TEXT NOT NULL,
  scope           VARCHAR(500) NOT NULL DEFAULT 'openid profile email',
  authorize_url   TEXT,
  token_url       TEXT,
  userinfo_url    TEXT,
  enabled         TINYINT(1) NOT NULL DEFAULT 1,
  created         DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated         DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  CHECK (kind IN ('oidc', 'oauth2'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS oauth_identities (
  id             CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  user_id        VARCHAR(255) NOT NULL,
  provider       VARCHAR(255) NOT NULL,
  external_id    VARCHAR(512) NOT NULL,
  email          TEXT,
  display_name   TEXT,
  raw_claims     JSON,
  last_login_at  DATETIME(6),
  created        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY uq_oauth_identity_provider_external (provider, external_id),
  KEY idx_oauth_identities_user (user_id),
  CONSTRAINT fk_oauth_identities_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_oauth_identities_provider FOREIGN KEY (provider) REFERENCES oauth_providers(name) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS oauth_sessions (
  session_id     VARCHAR(255) PRIMARY KEY,
  user_id        VARCHAR(255) NOT NULL,
  identity_id    CHAR(36),
  created        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at     DATETIME(6) NOT NULL,
  last_used_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  revoked        TINYINT(1) NOT NULL DEFAULT 0,
  user_agent     TEXT,
  ip_address     VARCHAR(45),
  revoked_at     DATETIME(6),
  KEY idx_oauth_sessions_user (user_id),
  KEY idx_oauth_sessions_expires (expires_at),
  CONSTRAINT fk_oauth_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_oauth_sessions_identity FOREIGN KEY (identity_id) REFERENCES oauth_identities(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS oauth_tokens (
  token          VARBINARY(255) PRIMARY KEY,
  user_id        VARCHAR(255) NOT NULL,
  provider       TEXT NOT NULL,
  scopes         JSON,
  expires_at     DATETIME(6),
  refresh_token  TEXT,
  created_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_used_at   DATETIME(6),
  KEY idx_oauth_tokens_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS oauth_state (
  state       VARBINARY(255) PRIMARY KEY,
  provider    TEXT NOT NULL,
  csrf_token  TEXT NOT NULL,
  return_url  TEXT,
  created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at  DATETIME(6) NOT NULL,
  KEY idx_oauth_state_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
  id           CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  url          TEXT NOT NULL,
  events       JSON NOT NULL,
  secret       TEXT NOT NULL,
  description  TEXT,
  owner_id     VARCHAR(255) NOT NULL DEFAULT 'default',
  namespace    VARCHAR(255) NOT NULL DEFAULT 'default',
  created      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  revoked      TINYINT(1) NOT NULL DEFAULT 0,
  revoked_at   DATETIME(6),
  KEY idx_webhook_subscriptions_owner (owner_id, namespace),
  CONSTRAINT fk_webhook_subscriptions_owner FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id                CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  subscription_id   CHAR(36) NOT NULL,
  event_type        TEXT NOT NULL,
  payload           LONGTEXT NOT NULL,
  payload_hash      VARCHAR(128) NOT NULL,
  attempt_num       INT NOT NULL DEFAULT 1,
  status            VARCHAR(32) NOT NULL DEFAULT 'pending',
  response_status   INT,
  response_body     TEXT,
  error             TEXT,
  scheduled_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  delivered_at      DATETIME(6),
  created           DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  created_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  lease_token       CHAR(36),
  lease_expires_at  DATETIME(6),
  writer_revision   INT DEFAULT 0,
  status_updated_at DATETIME(6),
  superseded        TINYINT(1) NOT NULL DEFAULT 0,
  KEY idx_webhook_deliveries_subscription (subscription_id),
  KEY idx_webhook_deliveries_scheduled (scheduled_at),
  KEY idx_webhook_deliveries_lease (lease_expires_at),
  KEY idx_webhook_deliveries_chain_attempt (subscription_id, payload_hash, attempt_num),
  KEY idx_webhook_deliveries_status_superseded (status, superseded),
  CONSTRAINT fk_webhook_deliveries_subscription FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS morpheus_runs (
  id                                 CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  started_at                         DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  finished_at                        DATETIME(6),
  status                             TEXT NOT NULL,
  phase                              TEXT,
  triggered_by                       TEXT NOT NULL,
  window_started_at                  DATETIME(6),
  window_ended_at                    DATETIME(6),
  window_hours                       INT NOT NULL DEFAULT 168,
  cluster_min_size                   INT NOT NULL DEFAULT 3,
  memories_scanned                   INT NOT NULL DEFAULT 0,
  clusters_found                     INT NOT NULL DEFAULT 0,
  summaries_created                  INT NOT NULL DEFAULT 0,
  error                              TEXT,
  config                             JSON NOT NULL DEFAULT (JSON_OBJECT()),
  namespace                          VARCHAR(255),
  memories_consolidated              INT NOT NULL DEFAULT 0,
  clusters_consolidated              INT NOT NULL DEFAULT 0,
  triples_extracted                  INT NOT NULL DEFAULT 0,
  memories_processed_for_extraction  INT NOT NULL DEFAULT 0,
  owner_id                           VARCHAR(255) NOT NULL DEFAULT 'default',
  KEY idx_morpheus_runs_status (status(64)),
  KEY idx_morpheus_runs_started (started_at),
  KEY idx_morpheus_runs_namespace (namespace)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS morpheus_extract_run_memories (
  run_id        CHAR(36) NOT NULL,
  memory_id     VARCHAR(255) NOT NULL,
  processed_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (run_id, memory_id),
  CONSTRAINT fk_merm_run FOREIGN KEY (run_id) REFERENCES morpheus_runs(id) ON DELETE CASCADE,
  CONSTRAINT fk_merm_memory FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS memory_archive (
  id                     VARCHAR(255) PRIMARY KEY,
  archived_at            DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  archived_by            TEXT NOT NULL,
  compressed_content     LONGBLOB NOT NULL,
  compression_algo       TEXT NOT NULL,
  original_size_bytes    INT NOT NULL,
  compressed_size_bytes  INT NOT NULL,
  schema_version         INT NOT NULL DEFAULT 1,
  CONSTRAINT fk_memory_archive_memory FOREIGN KEY (id) REFERENCES memories(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS deletion_requests (
  id                CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  target_user_id    VARCHAR(255) NOT NULL,
  target_namespace  VARCHAR(255),
  requested_by      VARCHAR(255) NOT NULL,
  requested_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  confirmed_at      DATETIME(6),
  soft_deleted_at   DATETIME(6),
  restore_by        DATETIME(6),
  restored_at       DATETIME(6),
  hard_deleted_at   DATETIME(6),
  status            VARCHAR(64) NOT NULL DEFAULT 'requested',
  notes             TEXT,
  KEY idx_deletion_requests_target (target_user_id, target_namespace),
  KEY idx_deletion_requests_status (status),
  CHECK (status IN ('requested','confirmed','sweep_verifying','soft_deleted','restored','hard_deleted','cancelled'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS deletion_log (
  id            CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  memory_id     VARCHAR(255) NOT NULL,
  content_hash  VARCHAR(128) NOT NULL,
  owner_id      VARCHAR(255),
  namespace     VARCHAR(255),
  requested_by  VARCHAR(255) NOT NULL,
  requested_at  DATETIME(6) NOT NULL,
  executed_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  request_kind  VARCHAR(64) NOT NULL,
  reason        TEXT,
  source        JSON,
  KEY idx_deletion_log_memory (memory_id),
  KEY idx_deletion_log_owner_namespace (owner_id, namespace),
  CHECK (request_kind IN ('gdpr_wipe','admin_purge','tombstone_collected'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS federation_peers (
  id                    CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  name                  VARCHAR(255) UNIQUE NOT NULL,
  base_url              VARCHAR(1024) NOT NULL,
  auth_token            TEXT NOT NULL,
  namespace_filter      JSON,
  category_filter       JSON,
  enabled               TINYINT(1) NOT NULL DEFAULT 1,
  sync_interval_secs    INT NOT NULL DEFAULT 300,
  last_sync_at          DATETIME(6),
  last_sync_cursor      DATETIME(6),
  last_error            TEXT,
  last_error_at         DATETIME(6),
  total_pulled          BIGINT NOT NULL DEFAULT 0,
  created               DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated               DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  compat_mode           VARCHAR(32) NOT NULL DEFAULT 'strict',
  peer_mnemos_version   TEXT,
  last_schema_check_at  DATETIME(6),
  copy_embeddings       SMALLINT NOT NULL DEFAULT 0,
  KEY idx_federation_peers_enabled (enabled, last_sync_at),
  KEY idx_federation_peers_base_url (base_url(255)),
  CHECK (sync_interval_secs >= 30),
  CHECK (copy_embeddings IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS federation_sync_log (
  id                CHAR(36) PRIMARY KEY DEFAULT (UUID()),
  peer_id           CHAR(36) NOT NULL,
  started_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  finished_at       DATETIME(6),
  memories_pulled   INT NOT NULL DEFAULT 0,
  memories_new      INT NOT NULL DEFAULT 0,
  memories_updated  INT NOT NULL DEFAULT 0,
  error             TEXT,
  cursor_before     DATETIME(6),
  cursor_after      DATETIME(6),
  direction         VARCHAR(16) NOT NULL DEFAULT 'pull',
  status            VARCHAR(32) NOT NULL DEFAULT 'started',
  records_seen      INT NOT NULL DEFAULT 0,
  records_written   INT NOT NULL DEFAULT 0,
  KEY idx_federation_sync_log_peer_started (peer_id, started_at),
  CONSTRAINT fk_federation_sync_log_peer FOREIGN KEY (peer_id) REFERENCES federation_peers(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
