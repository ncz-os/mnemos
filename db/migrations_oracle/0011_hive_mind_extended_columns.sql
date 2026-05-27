-- 0011_hive_mind_extended_columns.sql
-- GRAEAE Hive Mind — additive ALTERs to bring Oracle hive_* tables to
-- parity with the live SQLite shape at /srv/agent-bus/agents.db.
--
-- The original 0010_hive_mind.sql was authored before the live SQLite
-- accumulated 10 additional jobs columns + 4 agents columns through
-- runtime ALTERs. This migration closes the gap so the same Python
-- repository can target either backend without conditional SQL.
--
-- Idempotent: each ADD COLUMN guarded by user_tab_columns lookup;
-- re-running is a no-op. Apply with:
--   docker exec -i pythia-oracle bash -c "sqlplus -S mnemos/mnemos_dev@localhost:1521/ORCLPDB1 < /tmp/0011_hive_mind_extended_columns.sql"

WHENEVER SQLERROR EXIT FAILURE;

-- =====================================================================
-- hive_agents — add columns present in live SQLite agents table
-- =====================================================================
DECLARE
    v_count NUMBER;
    PROCEDURE add_col(p_col VARCHAR2, p_ddl VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count
          FROM user_tab_columns
         WHERE table_name = 'HIVE_AGENTS'
           AND column_name = UPPER(p_col);
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE hive_agents ADD (' || p_ddl || ')';
        END IF;
    END;
BEGIN
    add_col('auth_method',          'auth_method VARCHAR2(32)');
    add_col('plan_cap_usd',         'plan_cap_usd NUMBER(12,2)');
    add_col('plan_period_used_usd', 'plan_period_used_usd NUMBER(12,2) DEFAULT 0');
    add_col('current_load',         'current_load CLOB CHECK (current_load IS JSON)');
END;
/

-- =====================================================================
-- hive_jobs — add columns present in live SQLite jobs table
-- =====================================================================
DECLARE
    v_count NUMBER;
    PROCEDURE add_col(p_col VARCHAR2, p_ddl VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count
          FROM user_tab_columns
         WHERE table_name = 'HIVE_JOBS'
           AND column_name = UPPER(p_col);
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE hive_jobs ADD (' || p_ddl || ')';
        END IF;
    END;
BEGIN
    add_col('required_autonomy',   'required_autonomy VARCHAR2(32)');
    add_col('required_resources',  'required_resources CLOB CHECK (required_resources IS JSON)');
    add_col('claimed_host_caps',   'claimed_host_caps CLOB CHECK (claimed_host_caps IS JSON)');
    add_col('project',             'project VARCHAR2(120)');
    add_col('tags',                'tags CLOB CHECK (tags IS JSON)');
    add_col('depends_on',          'depends_on CLOB CHECK (depends_on IS JSON)');
    add_col('retry_count',         'retry_count NUMBER(5) DEFAULT 0 NOT NULL');
    add_col('max_retries',         'max_retries NUMBER(5) DEFAULT 2 NOT NULL');
    add_col('retry_backoff_until', 'retry_backoff_until TIMESTAMP WITH TIME ZONE');
    add_col('last_update_at',      'last_update_at TIMESTAMP WITH TIME ZONE');
    add_col('tokens_reasoning',    'tokens_reasoning NUMBER(15)');
    add_col('provider',            'provider VARCHAR2(64)');
    add_col('model',               'model VARCHAR2(128)');
    add_col('cost_usd_est',        'cost_usd_est NUMBER(12,6)');
END;
/

-- Indexes that the live SQLite has but Oracle 0010 did not include.
DECLARE
    e_exists      EXCEPTION;
    e_dup_col_idx EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
    PRAGMA EXCEPTION_INIT(e_dup_col_idx, -1408);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_hive_jobs_autonomy ON hive_jobs(required_autonomy)';
EXCEPTION
    WHEN e_exists THEN NULL;
    WHEN e_dup_col_idx THEN NULL;
END;
/
DECLARE
    e_exists      EXCEPTION;
    e_dup_col_idx EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
    PRAGMA EXCEPTION_INIT(e_dup_col_idx, -1408);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_hive_jobs_project ON hive_jobs(project)';
EXCEPTION
    WHEN e_exists THEN NULL;
    WHEN e_dup_col_idx THEN NULL;
END;
/
DECLARE
    e_exists      EXCEPTION;
    e_dup_col_idx EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
    PRAGMA EXCEPTION_INIT(e_dup_col_idx, -1408);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_hive_jobs_backoff ON hive_jobs(retry_backoff_until)';
EXCEPTION
    WHEN e_exists THEN NULL;
    WHEN e_dup_col_idx THEN NULL;
END;
/

-- =====================================================================
-- description column promotion: 0010 used VARCHAR2(4000); some job
-- descriptions (especially auto-generated handoffs) exceed 4 kB. Switch
-- to CLOB. Idempotent: only ALTER if current type is VARCHAR2.
-- =====================================================================
DECLARE
    v_type VARCHAR2(40);
BEGIN
    SELECT data_type INTO v_type
      FROM user_tab_columns
     WHERE table_name = 'HIVE_JOBS' AND column_name = 'DESCRIPTION';
    IF v_type = 'VARCHAR2' THEN
        -- two-step: copy → drop original → rename → done
        EXECUTE IMMEDIATE 'ALTER TABLE hive_jobs ADD (description_clob CLOB)';
        EXECUTE IMMEDIATE 'UPDATE hive_jobs SET description_clob = description WHERE description IS NOT NULL';
        EXECUTE IMMEDIATE 'ALTER TABLE hive_jobs DROP COLUMN description';
        EXECUTE IMMEDIATE 'ALTER TABLE hive_jobs RENAME COLUMN description_clob TO description';
    END IF;
END;
/

-- =====================================================================
-- worker_kind_stats + hive_cache + scheduled_jobs (Phase 2 spec — were
-- in my mnemos/hive_mind/oracle_ddl.sql but not 0010)
-- =====================================================================
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE hive_worker_kind_stats (
            agent_urn       VARCHAR2(256) NOT NULL,
            kind            VARCHAR2(64) NOT NULL,
            claims          NUMBER DEFAULT 0 NOT NULL,
            completions     NUMBER DEFAULT 0 NOT NULL,
            failures        NUMBER DEFAULT 0 NOT NULL,
            cancellations   NUMBER DEFAULT 0 NOT NULL,
            tokens_in       NUMBER DEFAULT 0 NOT NULL,
            tokens_out      NUMBER DEFAULT 0 NOT NULL,
            est_cost_usd    NUMBER(14,6) DEFAULT 0 NOT NULL,
            total_duration_sec NUMBER(14,3) DEFAULT 0 NOT NULL,
            last_seen_at    TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_hive_worker_kind_stats PRIMARY KEY (agent_urn, kind)
        )
    ]';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_hive_wks_kind ON hive_worker_kind_stats(kind)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE hive_cache (
            cache_key       VARCHAR2(500) PRIMARY KEY,
            source_job_id   VARCHAR2(64),
            value           CLOB CHECK (value IS JSON),
            provider        VARCHAR2(64),
            model           VARCHAR2(128),
            result_mnemos_id VARCHAR2(64),
            hit_count       NUMBER DEFAULT 0 NOT NULL,
            cost_saved_usd  NUMBER(14,6) DEFAULT 0 NOT NULL,
            stored_at       TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
            expires_at      TIMESTAMP WITH TIME ZONE
        )
    ]';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_hive_cache_expires ON hive_cache(expires_at)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE hive_scheduled_jobs (
            id              VARCHAR2(64) PRIMARY KEY,
            owner_urn       VARCHAR2(256) NOT NULL,
            cron_expr       VARCHAR2(120),
            run_at          TIMESTAMP WITH TIME ZONE,
            payload         CLOB NOT NULL CHECK (payload IS JSON),
            enabled         NUMBER(1) DEFAULT 1 NOT NULL CHECK (enabled IN (0,1)),
            last_run_at     TIMESTAMP WITH TIME ZONE,
            last_run_status VARCHAR2(20),
            next_run_at     TIMESTAMP WITH TIME ZONE NOT NULL,
            created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    ]';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_hive_sched_next ON hive_scheduled_jobs(enabled, next_run_at)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
