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
-- Idempotency + drift reconciliation: an earlier migration
-- (0011_hive_mind_extended_columns) also creates HIVE_SCHEDULED_JOBS with
-- a now-superseded shape (owner_urn / run_at / next_run_at / cron_expr).
-- On a clean deploy 0011 runs first and wins the CREATE, so this
-- migration (1) creates the canonical table only when absent,
-- (2) reconciles missing columns onto a pre-existing table, and
-- (3) builds each index only when its drift-prone target column exists.

-- (1) Create the canonical table only when no HIVE_SCHEDULED_JOBS exists.
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

-- (2) Reconcile column drift: add canonical columns the 0011 shape lacks.
-- Added nullable so the ALTER is safe regardless of existing rows; this
-- is scheduler state with no ORM consumer in mnemos.
DECLARE
  v_count NUMBER;
  PROCEDURE add_col(p_col VARCHAR2, p_ddl VARCHAR2) IS
  BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tab_columns
     WHERE table_name = 'HIVE_SCHEDULED_JOBS' AND column_name = UPPER(p_col);
    IF v_count = 0 THEN
      EXECUTE IMMEDIATE 'ALTER TABLE hive_scheduled_jobs ADD (' || p_ddl || ')';
    END IF;
  END;
BEGIN
  add_col('name',             'name VARCHAR2(256)');
  add_col('created_by_urn',   'created_by_urn VARCHAR2(256)');
  add_col('interval_seconds', 'interval_seconds NUMBER(12)');
  add_col('job_template',     'job_template JSON');
  add_col('last_fired_at',    'last_fired_at NUMBER');
  add_col('next_fire_at',     'next_fire_at NUMBER');
  add_col('fire_count',       'fire_count NUMBER(15) DEFAULT 0');
END;
/

-- (3) Indexes: create only when the index is absent AND its drift-prone
-- target column exists (enabled is present in both shapes, so guarding on
-- next_fire_at / created_by_urn is sufficient to avoid ORA-00904).
DECLARE
  PROCEDURE create_index(p_name VARCHAR2, p_col VARCHAR2, p_ddl VARCHAR2) IS
    v_i NUMBER;
    v_c NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_i FROM user_indexes WHERE index_name = p_name;
    SELECT COUNT(*) INTO v_c FROM user_tab_columns
     WHERE table_name = 'HIVE_SCHEDULED_JOBS' AND column_name = UPPER(p_col);
    IF v_i = 0 AND v_c > 0 THEN EXECUTE IMMEDIATE p_ddl; END IF;
  END;
BEGIN
  -- Tick path: heavily used by scheduler. (next_fire_at, enabled) lets
  -- the planner do a single index range scan filtering enabled=1.
  create_index('IX_HIVE_SCHEDULED_NEXT', 'next_fire_at',
               'CREATE INDEX ix_hive_scheduled_next ON hive_scheduled_jobs(next_fire_at, enabled)');
  create_index('IX_HIVE_SCHEDULED_CREATOR', 'created_by_urn',
               'CREATE INDEX ix_hive_scheduled_creator ON hive_scheduled_jobs(created_by_urn)');
END;
/

COMMIT;
