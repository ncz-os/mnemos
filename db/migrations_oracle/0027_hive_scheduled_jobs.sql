-- migration: 0027_hive_scheduled_jobs
-- target:    Oracle 23ai PDB ORCLPDB1
-- schema:    HIVE_MIND
-- purpose:   Recurring job templates. A scheduler tick scans
--            next_fire_at <= now() AND enabled = 1, materializes a real
--            jobs row from job_template, advances next_fire_at by
--            interval_seconds. Mirrors /srv/agent-bus/agents.db `scheduled_jobs`.
--
-- Notes:
--   - job_template is the full job-submit JSON the scheduler POSTs.
--   - interval_seconds is the period; first fire respects next_fire_at.
--   - enabled is NUMBER(1) (1=on, 0=off) — matches SQLite INTEGER bool.
--   - fire_count + last_fired_at drive cadence dashboards.
--
-- Idempotency: guarded by USER_TABLES.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'HIVE_SCHEDULED_JOBS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hive_scheduled_jobs (
        id                VARCHAR2(64)   NOT NULL,
        name              VARCHAR2(256)  NOT NULL,
        created_by_urn    VARCHAR2(256)  NOT NULL,
        interval_seconds  NUMBER(12)     NOT NULL,
        job_template      JSON           NOT NULL,
        enabled           NUMBER(1)      DEFAULT 1 NOT NULL,
        last_fired_at     NUMBER,
        next_fire_at      NUMBER         NOT NULL,
        fire_count        NUMBER(15)     DEFAULT 0 NOT NULL,
        created_at        NUMBER         NOT NULL,
        CONSTRAINT pk_hive_scheduled PRIMARY KEY (id),
        CONSTRAINT ck_hive_scheduled_enabled CHECK (enabled IN (0,1))
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
  -- Tick path: heavily used by scheduler. (next_fire_at, enabled) lets
  -- the planner do a single index range scan filtering enabled=1.
  create_index('IX_HIVE_SCHEDULED_NEXT',
               'CREATE INDEX ix_hive_scheduled_next ON hive_scheduled_jobs(next_fire_at, enabled)');
  create_index('IX_HIVE_SCHEDULED_CREATOR',
               'CREATE INDEX ix_hive_scheduled_creator ON hive_scheduled_jobs(created_by_urn)');
END;
/

COMMIT;
