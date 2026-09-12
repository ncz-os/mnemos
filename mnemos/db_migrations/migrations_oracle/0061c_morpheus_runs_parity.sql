-- 0061c_morpheus_runs_parity.sql — Oracle 23ai retcon for MNEMOS parity
-- (item 11/11a).
--
-- The pre-11a Oracle/DB2 ``morpheus_runs`` shape was an abandoned early
-- iteration: ``id, run_type, status, started_at, finished_at, metrics,
-- error``. It was never reconciled with the canonical Postgres shape:
--
--   id, started_at, finished_at, status, phase, triggered_by,
--   window_started_at, window_ended_at, window_hours, cluster_min_size,
--   memories_scanned, clusters_found, summaries_created,
--   memories_consolidated, clusters_consolidated,
--   triples_extracted, memories_processed_for_extraction,
--   error, config, namespace
--
-- Same shape drift pattern that item 9 retconned on
-- ``nats_dispatch_log``: legacy columns were never read by any
-- production code (grep ``morpheus_runs`` across the repo confirms only
-- ``mnemos/domain/morpheus/runner.py`` and these migrations touch the
-- table; nothing references ``run_type`` or ``metrics``). The retcon
-- drops those columns and adds the canonical ones.
--
-- Oracle's ``morpheus_runs.id`` is currently VARCHAR2(36) (no
-- gen_random_uuid default). We keep VARCHAR2(36) and let the Python
-- repository layer generate the UUID on begin_run, matching the
-- Postgres canonical-shape contract from
-- ``mnemos/db_migrations/migrations_v3_3_morpheus.sql``.

-- ── 1. add missing columns. ADD COLUMN is idempotent in Oracle; the
--       migration applier swallows ORA-01430 / "column already exists"
--       as a benign-replay signal so re-runs are safe.

ALTER TABLE morpheus_runs ADD (phase VARCHAR2(64));
ALTER TABLE morpheus_runs ADD (triggered_by VARCHAR2(32) DEFAULT 'cron' NOT NULL);

ALTER TABLE morpheus_runs ADD (window_started_at TIMESTAMP WITH TIME ZONE);
ALTER TABLE morpheus_runs ADD (window_ended_at   TIMESTAMP WITH TIME ZONE);
ALTER TABLE morpheus_runs ADD (window_hours      NUMBER(10) DEFAULT 168 NOT NULL);
ALTER TABLE morpheus_runs ADD (cluster_min_size  NUMBER(10) DEFAULT 3 NOT NULL);

ALTER TABLE morpheus_runs ADD (memories_scanned    NUMBER(19) DEFAULT 0 NOT NULL);
ALTER TABLE morpheus_runs ADD (clusters_found      NUMBER(19) DEFAULT 0 NOT NULL);
ALTER TABLE morpheus_runs ADD (summaries_created   NUMBER(19) DEFAULT 0 NOT NULL);

ALTER TABLE morpheus_runs ADD (memories_consolidated NUMBER(19) DEFAULT 0 NOT NULL);
ALTER TABLE morpheus_runs ADD (clusters_consolidated NUMBER(19) DEFAULT 0 NOT NULL);

ALTER TABLE morpheus_runs ADD (triples_extracted               NUMBER(19) DEFAULT 0 NOT NULL);
ALTER TABLE morpheus_runs ADD (memories_processed_for_extraction NUMBER(19) DEFAULT 0 NOT NULL);

-- Canonical shape uses CLOB CHECK (... IS JSON) for ``config`` (Oracle
-- 23ai's native JSON validation). The legacy table had ``metrics CLOB
-- CHECK (metrics IS JSON)``; we drop ``metrics`` and add ``config``.
ALTER TABLE morpheus_runs ADD (config CLOB CHECK (config IS JSON));
ALTER TABLE morpheus_runs ADD (namespace VARCHAR2(256));

-- ── 2. drop legacy ``run_type`` + ``metrics`` columns (Oracle 23ai
--       supports DROP COLUMN). Greps confirm neither column is read by
--       any production path; ``morpheus_runs.run_type`` was an early
--       "what kind of dream is this" label that never reconciled with
--       the Postgres canonical ``phase`` + ``triggered_by`` split.
ALTER TABLE morpheus_runs DROP COLUMN run_type;
ALTER TABLE morpheus_runs DROP COLUMN metrics;

-- ── 3. align status default to 'running' (Postgres canonical).
ALTER TABLE morpheus_runs MODIFY (status DEFAULT 'running');

-- ── 4. CHECK constraints. Wrap the constraint drop in a PL/SQL block
--       so the migration is idempotent — Oracle lacks a native
--       ``DROP CONSTRAINT IF EXISTS`` and the migration applier only
--       swallows ORA-00955/02275/01430/04081, not ORA-02443.
--
--       PL/SQL terminator for the sqlplus-style applier is ``/`` on
--       its own line (see scripts/oracle_apply_migration.py).
DECLARE
    already_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(already_exists, -02264);  -- ORA-02264 name already used
BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE morpheus_runs DROP CONSTRAINT morpheus_runs_status_check';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE NOT IN (-02443 /* constraint does not exist */) THEN
            RAISE;
        END IF;
END;
/

ALTER TABLE morpheus_runs ADD CONSTRAINT morpheus_runs_status_check
    CHECK (status IN ('running','success','failed','rolled_back'));

DECLARE
    already_exists EXCEPTION;
    PRAGMA EXCEPTION_INIT(already_exists, -02264);
BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE morpheus_runs DROP CONSTRAINT morpheus_runs_triggered_by_check';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE NOT IN (-02443) THEN
            RAISE;
        END IF;
END;
/

ALTER TABLE morpheus_runs ADD CONSTRAINT morpheus_runs_triggered_by_check
    CHECK (triggered_by IN ('cron','manual','api'));

-- ── 5. indexes — Postgres ships
--         idx_morpheus_runs_status  ON morpheus_runs(status)
--         idx_morpheus_runs_started ON morpheus_runs(started_at DESC)
--         idx_morpheus_runs_namespace ON morpheus_runs(namespace) WHERE namespace IS NOT NULL
--       Oracle already has idx_morpheus_runs_status + idx_morpheus_runs_started
--       from the original 0009_morpheus_runs.sql; just add the namespace one.
CREATE INDEX idx_morpheus_runs_namespace ON morpheus_runs (namespace);