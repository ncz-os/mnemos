--#SET TERMINATOR @
-- migration: 0022_hive_jobs
-- target:    IBM Db2 12.1.5
-- purpose:   Hive Mind job queue (Db2 variant). Mirrors PG + Oracle.
--            Dequeue uses Db2's "FOR UPDATE WITH RS USE AND KEEP UPDATE LOCKS"
--            or "SKIP LOCKED DATA" (12.1+). Application layer chooses.

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE hive_jobs (
      id                       VARCHAR(64)    NOT NULL,
      submitter_urn            VARCHAR(256)   NOT NULL,
      parent_job_id            VARCHAR(64),
      kind                     VARCHAR(256)   NOT NULL,
      title                    VARCHAR(512),
      description              CLOB(2M),
      priority                 INTEGER        DEFAULT 0 NOT NULL,
      deadline                 DOUBLE,
      required_capabilities    CLOB(1M) INLINE LENGTH 1024
        CHECK (SYSTOOLS.JSON2BSON(required_capabilities) IS NOT NULL),
      eligible_kinds           CLOB(1M) INLINE LENGTH 1024
        CHECK (SYSTOOLS.JSON2BSON(eligible_kinds) IS NOT NULL),
      status                   VARCHAR(16)    NOT NULL,
      claimed_by               VARCHAR(256),
      claimed_at               DOUBLE,
      started_at               DOUBLE NOT NULL,
      ended_at                 DOUBLE,
      result                   CLOB(4M) INLINE LENGTH 1024
        CHECK (SYSTOOLS.JSON2BSON(result) IS NOT NULL),
      required_autonomy        VARCHAR(32),
      max_cost_tier            VARCHAR(2),
      preferred_providers      CLOB(1M) INLINE LENGTH 1024,
      preferred_models         CLOB(1M) INLINE LENGTH 1024,
      claimed_runtime          VARCHAR(64),
      claimed_model            VARCHAR(128),
      claimed_provider         VARCHAR(64),
      claimed_cost_tier        VARCHAR(2),
      tokens_in                BIGINT,
      tokens_out               BIGINT,
      estimated_cost_usd       DECIMAL(12, 6),
      mnemos_refs              CLOB(1M) INLINE LENGTH 1024,
      result_mnemos_id         VARCHAR(64),
      required_resources       CLOB(1M) INLINE LENGTH 1024,
      claimed_host_caps        CLOB(1M) INLINE LENGTH 1024,
      project                  VARCHAR(64),
      tags                     CLOB(1M) INLINE LENGTH 1024,
      depends_on               CLOB(1M) INLINE LENGTH 1024,
      retry_count              INTEGER        DEFAULT 0 NOT NULL,
      max_retries              INTEGER        DEFAULT 2 NOT NULL,
      retry_backoff_until      DOUBLE,
      last_update_at           DOUBLE,
      CONSTRAINT pk_hive_jobs PRIMARY KEY (id),
      CONSTRAINT ck_hive_jobs_status
        CHECK (status IN (''queued'',''offered'',''claimed'',''running'',
                          ''done'',''failed'',''cancelled''))
    )';
END@

BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_jobs_queue ON hive_jobs(status, priority DESC, started_at ASC)';
END@
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_jobs_submitter ON hive_jobs(submitter_urn)';
END@
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_jobs_claimed_by ON hive_jobs(claimed_by)';
END@
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_jobs_parent ON hive_jobs(parent_job_id)';
END@
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_jobs_project ON hive_jobs(project)';
END@
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_jobs_backoff ON hive_jobs(retry_backoff_until)';
END@
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_jobs_kind_status ON hive_jobs(kind, status)';
END@
