-- migration: 0002_hive_jobs
-- target:    Oracle 23ai PDB ORCLPDB1
-- schema:    HIVE_MIND
-- purpose:   Hive Mind job queue. Phase 2 SQLite -> Oracle port.
--
-- Critical correctness:
--   - dequeue uses SELECT ... FOR UPDATE SKIP LOCKED for atomic claim
--     under contention. Matches mnemos-prod-working/db/migrations_oracle/
--     pattern + Oracle 23ai's queue-friendly locking model.
--   - jobs.status CHECK constraint enforces FSM.
--   - ix_hive_jobs_queue is the dequeue path: (status, priority DESC,
--     started_at ASC) — Polars dequeue snapshots filter on this.
--   - JSON columns: required_capabilities, eligible_kinds,
--     preferred_providers, preferred_models, mnemos_refs,
--     required_resources, claimed_host_caps, tags, depends_on, result.
--
-- Idempotency: guarded by USER_TABLES.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'HIVE_JOBS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hive_jobs (
        id                       VARCHAR2(64)    NOT NULL,
        submitter_urn            VARCHAR2(256)   NOT NULL,
        parent_job_id            VARCHAR2(64),
        kind                     VARCHAR2(256)   NOT NULL,
        title                    VARCHAR2(512),
        description              CLOB,
        priority                 NUMBER(5)       DEFAULT 0 NOT NULL,
        deadline                 NUMBER,
        required_capabilities    JSON,
        eligible_kinds           JSON,
        status                   VARCHAR2(16)    NOT NULL,
        claimed_by               VARCHAR2(256),
        claimed_at               NUMBER,
        started_at               NUMBER          NOT NULL,
        ended_at                 NUMBER,
        result                   JSON,
        required_autonomy        VARCHAR2(32),
        max_cost_tier            VARCHAR2(2),
        preferred_providers      JSON,
        preferred_models         JSON,
        claimed_runtime          VARCHAR2(64),
        claimed_model            VARCHAR2(128),
        claimed_provider         VARCHAR2(64),
        claimed_cost_tier        VARCHAR2(2),
        tokens_in                NUMBER(12),
        tokens_out               NUMBER(12),
        estimated_cost_usd       NUMBER(12, 6),
        mnemos_refs              JSON,
        result_mnemos_id         VARCHAR2(64),
        required_resources       JSON,
        claimed_host_caps        JSON,
        project                  VARCHAR2(64),
        tags                     JSON,
        depends_on               JSON,
        retry_count              NUMBER(5)       DEFAULT 0 NOT NULL,
        max_retries              NUMBER(5)       DEFAULT 2 NOT NULL,
        retry_backoff_until      NUMBER,
        last_update_at           NUMBER,
        CONSTRAINT pk_hive_jobs PRIMARY KEY (id),
        CONSTRAINT ck_hive_jobs_status
          CHECK (status IN ('queued','offered','claimed','running',
                            'done','failed','cancelled'))
      )
    ]';
  END IF;
END;
/

DECLARE
  PROCEDURE create_index(p_name VARCHAR2, p_ddl VARCHAR2) IS
    v_n NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_n FROM user_indexes WHERE index_name = p_name;
    IF v_n = 0 THEN EXECUTE IMMEDIATE p_ddl; END IF;
  END;
BEGIN
  -- Dequeue path: heavily used by dequeue_next_job. Compound index
  -- (status, priority DESC, started_at ASC) lets the planner do a
  -- single index range scan when filtering status='queued'.
  create_index('IX_HIVE_JOBS_QUEUE',
               'CREATE INDEX ix_hive_jobs_queue ON hive_jobs(status, priority DESC, started_at ASC)');
  create_index('IX_HIVE_JOBS_SUBMITTER',
               'CREATE INDEX ix_hive_jobs_submitter ON hive_jobs(submitter_urn)');
  create_index('IX_HIVE_JOBS_CLAIMED_BY',
               'CREATE INDEX ix_hive_jobs_claimed_by ON hive_jobs(claimed_by)');
  create_index('IX_HIVE_JOBS_PARENT',
               'CREATE INDEX ix_hive_jobs_parent ON hive_jobs(parent_job_id)');
  create_index('IX_HIVE_JOBS_PROJECT',
               'CREATE INDEX ix_hive_jobs_project ON hive_jobs(project)');
  create_index('IX_HIVE_JOBS_BACKOFF',
               'CREATE INDEX ix_hive_jobs_backoff ON hive_jobs(retry_backoff_until)');
  create_index('IX_HIVE_JOBS_KIND_STATUS',
               'CREATE INDEX ix_hive_jobs_kind_status ON hive_jobs(kind, status)');
END;
/

COMMIT;
