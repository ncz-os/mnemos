-- migration: 0022_hive_jobs
-- target:    PostgreSQL 16
-- schema:    public
-- purpose:   Hive Mind job queue (PG variant). Mirrors
--            db/migrations_oracle/0022_hive_jobs.sql.
--
-- Dequeue uses PG's standard SELECT ... FOR UPDATE SKIP LOCKED.

CREATE TABLE IF NOT EXISTS hive_jobs (
  id                       VARCHAR(64)    NOT NULL,
  submitter_urn            VARCHAR(256)   NOT NULL,
  parent_job_id            VARCHAR(64),
  kind                     VARCHAR(256)   NOT NULL,
  title                    VARCHAR(512),
  description              TEXT,
  priority                 INTEGER        DEFAULT 0 NOT NULL,
  deadline                 DOUBLE PRECISION,
  required_capabilities    JSONB,
  eligible_kinds           JSONB,
  status                   VARCHAR(16)    NOT NULL,
  claimed_by               VARCHAR(256),
  claimed_at               DOUBLE PRECISION,
  started_at               DOUBLE PRECISION NOT NULL,
  ended_at                 DOUBLE PRECISION,
  result                   JSONB,
  required_autonomy        VARCHAR(32),
  max_cost_tier            VARCHAR(2),
  preferred_providers      JSONB,
  preferred_models         JSONB,
  claimed_runtime          VARCHAR(64),
  claimed_model            VARCHAR(128),
  claimed_provider         VARCHAR(64),
  claimed_cost_tier        VARCHAR(2),
  tokens_in                BIGINT,
  tokens_out               BIGINT,
  estimated_cost_usd       NUMERIC(12, 6),
  mnemos_refs              JSONB,
  result_mnemos_id         VARCHAR(64),
  required_resources       JSONB,
  claimed_host_caps        JSONB,
  project                  VARCHAR(64),
  tags                     JSONB,
  depends_on               JSONB,
  retry_count              INTEGER        DEFAULT 0 NOT NULL,
  max_retries              INTEGER        DEFAULT 2 NOT NULL,
  retry_backoff_until      DOUBLE PRECISION,
  last_update_at           DOUBLE PRECISION,
  CONSTRAINT pk_hive_jobs PRIMARY KEY (id),
  CONSTRAINT ck_hive_jobs_status
    CHECK (status IN ('queued','offered','claimed','running',
                      'done','failed','cancelled'))
);

CREATE INDEX IF NOT EXISTS ix_hive_jobs_queue       ON hive_jobs(status, priority DESC, started_at ASC);
CREATE INDEX IF NOT EXISTS ix_hive_jobs_submitter   ON hive_jobs(submitter_urn);
CREATE INDEX IF NOT EXISTS ix_hive_jobs_claimed_by  ON hive_jobs(claimed_by);
CREATE INDEX IF NOT EXISTS ix_hive_jobs_parent      ON hive_jobs(parent_job_id);
CREATE INDEX IF NOT EXISTS ix_hive_jobs_project     ON hive_jobs(project);
CREATE INDEX IF NOT EXISTS ix_hive_jobs_backoff     ON hive_jobs(retry_backoff_until);
CREATE INDEX IF NOT EXISTS ix_hive_jobs_kind_status ON hive_jobs(kind, status);
