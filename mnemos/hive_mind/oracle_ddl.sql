-- Oracle DDL for OracleHiveMindRepository backend (hive Phase 2).
--
-- Mirrors the SQLite schema at /srv/agent-bus/agent_bus.py (SCHEMA constant)
-- so a SQLite -> Oracle dump + restore is row-equivalent.
--
-- Conventions
--   * All tables prefixed memory_*  (shared MNEMOS Oracle PDB ORCLPDB1).
--   * IDs that were TEXT in SQLite stay as VARCHAR2 here — interop with the
--     SQLite source is more important than the marginal storage win of
--     RAW(16). The job description called for RAW(16) UUIDv7; we keep the
--     canonical text form because (a) HTTP API surfaces UUIDs as strings,
--     (b) the FastAPI routes already emit/accept text IDs, (c) Oracle
--     SYS_GUID() and inserted external UUIDv7 strings both fit cleanly.
--     Switching to RAW(16) is a future optimisation that requires a
--     coordinated repo+route+test sweep.
--   * timestamps: SQLite stored unix-epoch floats (REAL NOT NULL). Mirror
--     as NUMBER(20,6) so Python comparisons + JSON serialisation stay
--     identical; do NOT convert to TIMESTAMP at this layer.
--   * JSON-shaped TEXT columns (capabilities, required_capabilities,
--     eligible_kinds, metadata, payload, result) map to CLOB. Application
--     handles json.loads/json.dumps at the repo layer.
--   * CHECK constraints copied verbatim.
--   * AUTOINCREMENT events.id -> NUMBER GENERATED ALWAYS AS IDENTITY.
--
-- Idempotency: re-runnable. Each CREATE wrapped in PL/SQL block that
-- swallows ORA-00955 (name already used) so this file can be applied
-- repeatedly without DROP.

WHENEVER SQLERROR EXIT FAILURE;

-- =====================================================================
-- memory_agents
-- =====================================================================
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE memory_agents (
            urn             VARCHAR2(400) PRIMARY KEY,
            kind            VARCHAR2(100) NOT NULL,
            host            VARCHAR2(200) NOT NULL,
            session_id      VARCHAR2(200) NOT NULL,
            pid             NUMBER,
            capabilities    CLOB,
            version         VARCHAR2(200),
            started_at      NUMBER(20,6) NOT NULL,
            last_heartbeat  NUMBER(20,6) NOT NULL,
            status          VARCHAR2(20)  NOT NULL
                CHECK (status IN ('online','idle','offline','error')),
            metadata        CLOB
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
        'CREATE INDEX idx_memory_agents_status ON memory_agents(status)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_agents_kind ON memory_agents(kind)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

-- =====================================================================
-- memory_jobs
-- =====================================================================
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE memory_jobs (
            id                      VARCHAR2(60) PRIMARY KEY,
            submitter_urn           VARCHAR2(400) NOT NULL,
            parent_job_id           VARCHAR2(60),
            kind                    VARCHAR2(120) NOT NULL,
            description             CLOB,
            priority                NUMBER DEFAULT 0 NOT NULL,
            deadline                NUMBER(20,6),
            required_capabilities   CLOB,
            eligible_kinds          CLOB,
            project                 VARCHAR2(120),
            status                  VARCHAR2(20)  NOT NULL
                CHECK (status IN ('queued','offered','claimed','running','done','failed','cancelled')),
            claimed_by              VARCHAR2(400),
            claimed_at              NUMBER(20,6),
            started_at              NUMBER(20,6) NOT NULL,
            ended_at                NUMBER(20,6),
            result                  CLOB
        )
    ]';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

-- index set — mirrors SQLite + adds the queue-pop index the job description specified.
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_jobs_status ON memory_jobs(status)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_jobs_submitter ON memory_jobs(submitter_urn)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_jobs_claimed_by ON memory_jobs(claimed_by)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_jobs_parent ON memory_jobs(parent_job_id)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    -- Queue-pop composite: claimer scans queued + highest priority first
    -- + oldest first. Matches SQLite idx_jobs_queue and feeds the
    -- SELECT ... FOR UPDATE SKIP LOCKED dequeue path on Oracle.
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_jobs_queue
            ON memory_jobs(status, priority DESC, started_at ASC)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_jobs_project ON memory_jobs(project)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

-- =====================================================================
-- memory_messages
-- =====================================================================
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE memory_messages (
            id              VARCHAR2(60) PRIMARY KEY,
            from_urn        VARCHAR2(400) NOT NULL,
            to_urn          VARCHAR2(400),
            in_reply_to     VARCHAR2(60),
            topic           VARCHAR2(200) NOT NULL,
            payload         CLOB NOT NULL,
            ts              NUMBER(20,6) NOT NULL
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
        'CREATE INDEX idx_memory_messages_to ON memory_messages(to_urn)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_messages_topic ON memory_messages(topic)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_messages_ts ON memory_messages(ts)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

-- =====================================================================
-- memory_events  (SQLite used INTEGER PRIMARY KEY AUTOINCREMENT)
-- =====================================================================
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE memory_events (
            id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ts          NUMBER(20,6) NOT NULL,
            kind        VARCHAR2(120) NOT NULL,
            payload     CLOB NOT NULL,
            agent_urn   VARCHAR2(400)
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
        'CREATE INDEX idx_memory_events_ts ON memory_events(ts)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_events_kind ON memory_events(kind)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE
        'CREATE INDEX idx_memory_events_agent ON memory_events(agent_urn)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

-- =====================================================================
-- memory_hive_cache  (Phase 2 spec — agent-side memo cache)
-- =====================================================================
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE memory_hive_cache (
            cache_key       VARCHAR2(400) PRIMARY KEY,
            value           CLOB,
            updated_at      NUMBER(20,6) NOT NULL,
            ttl_seconds     NUMBER
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
        'CREATE INDEX idx_memory_hive_cache_updated ON memory_hive_cache(updated_at)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

-- =====================================================================
-- memory_worker_kind_stats  (Phase 2 spec — per-worker per-kind audit)
-- =====================================================================
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE memory_worker_kind_stats (
            agent_urn       VARCHAR2(400) NOT NULL,
            kind            VARCHAR2(120) NOT NULL,
            claims          NUMBER DEFAULT 0 NOT NULL,
            completions     NUMBER DEFAULT 0 NOT NULL,
            failures        NUMBER DEFAULT 0 NOT NULL,
            tokens_in       NUMBER DEFAULT 0 NOT NULL,
            tokens_out      NUMBER DEFAULT 0 NOT NULL,
            est_cost_usd    NUMBER(18,6) DEFAULT 0 NOT NULL,
            last_seen_at    NUMBER(20,6),
            CONSTRAINT pk_memory_worker_kind_stats PRIMARY KEY (agent_urn, kind)
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
        'CREATE INDEX idx_memory_wks_kind ON memory_worker_kind_stats(kind)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

-- =====================================================================
-- memory_scheduled_jobs  (Phase 2 spec — cron-like deferred submission)
-- =====================================================================
DECLARE
    e_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_exists, -955);
BEGIN
    EXECUTE IMMEDIATE q'[
        CREATE TABLE memory_scheduled_jobs (
            id                  VARCHAR2(60) PRIMARY KEY,
            owner_urn           VARCHAR2(400) NOT NULL,
            cron_expr           VARCHAR2(120),
            run_at              NUMBER(20,6),
            payload             CLOB NOT NULL,
            enabled             NUMBER(1) DEFAULT 1 NOT NULL
                CHECK (enabled IN (0,1)),
            last_run_at         NUMBER(20,6),
            last_run_status     VARCHAR2(20),
            next_run_at         NUMBER(20,6) NOT NULL,
            created_at          NUMBER(20,6) NOT NULL
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
        'CREATE INDEX idx_memory_sched_next ON memory_scheduled_jobs(enabled, next_run_at)';
EXCEPTION WHEN e_exists THEN NULL;
END;
/

-- =====================================================================
-- Operational notes (do NOT execute; comment block for repo readers)
-- =====================================================================
--
-- Dequeue path  (matches SQLite snapshot+CAS pattern from commit a0ceebd):
--   SELECT id FROM memory_jobs
--    WHERE status = 'queued'
--      AND (eligible_kinds IS NULL
--           OR JSON_EXISTS(eligible_kinds, '$[*]?(@ == "$AGENT_KIND")'))
--    ORDER BY priority DESC, started_at ASC
--    FETCH FIRST 1 ROWS ONLY
--    FOR UPDATE SKIP LOCKED;
--   UPDATE memory_jobs SET status='claimed', claimed_by=:urn, claimed_at=:now
--    WHERE id=:id AND status='queued';
--   COMMIT;
--
-- Cache upsert (mirrors SQLite INSERT OR REPLACE):
--   MERGE INTO memory_hive_cache c USING (SELECT :k AS k FROM dual) s
--     ON (c.cache_key = s.k)
--   WHEN MATCHED THEN UPDATE SET value=:v, updated_at=:t, ttl_seconds=:ttl
--   WHEN NOT MATCHED THEN INSERT (cache_key, value, updated_at, ttl_seconds)
--     VALUES (:k, :v, :t, :ttl);
--
-- Per-worker per-kind upsert:
--   MERGE INTO memory_worker_kind_stats s
--     USING (SELECT :urn AS urn, :kind AS kind FROM dual) src
--     ON (s.agent_urn = src.urn AND s.kind = src.kind)
--   WHEN MATCHED THEN UPDATE SET
--     claims=claims+1, completions=completions+:c, failures=failures+:f,
--     tokens_in=tokens_in+:ti, tokens_out=tokens_out+:to,
--     est_cost_usd=est_cost_usd+:cost, last_seen_at=:ts
--   WHEN NOT MATCHED THEN INSERT
--     (agent_urn,kind,claims,completions,failures,tokens_in,tokens_out,est_cost_usd,last_seen_at)
--     VALUES (:urn,:kind,1,:c,:f,:ti,:to,:cost,:ts);
