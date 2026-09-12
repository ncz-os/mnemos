--#SET TERMINATOR @
-- 0061c_morpheus_runs_parity.sql — Db2 12.1.5 (Oracle Compat mode) port
-- for MNEMOS parity (item 11/11a).
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
-- IMPORTANT: this file uses ``@`` as the ONLY statement terminator.
-- ``split_db2_statements`` (mnemos/persistence/schema.py) switches the
-- ENTIRE file to ``@``-only splitting the moment any line ends with
-- ``@`` (``_uses_db2_at_terminator``) — a mix of ``;`` and ``@`` in one
-- file is not supported and silently concatenates every ``;``-terminated
-- statement into one invalid multi-statement blob. Every statement
-- below, including the plain ``ALTER TABLE`` ones, MUST end with a bare
-- ``@`` on its own line, never ``;``.

-- ── 1. add missing columns. ADD COLUMN is idempotent in Db2; the
--       migration applier swallows SQLSTATE 42711 (column already
--       exists) as a benign-replay signal so re-runs are safe.

ALTER TABLE morpheus_runs ADD COLUMN phase VARCHAR(64)
@
ALTER TABLE morpheus_runs ADD COLUMN triggered_by VARCHAR(32) DEFAULT 'cron' NOT NULL
@

ALTER TABLE morpheus_runs ADD COLUMN window_started_at TIMESTAMP
@
ALTER TABLE morpheus_runs ADD COLUMN window_ended_at   TIMESTAMP
@
ALTER TABLE morpheus_runs ADD COLUMN window_hours      BIGINT DEFAULT 168 NOT NULL
@
ALTER TABLE morpheus_runs ADD COLUMN cluster_min_size  BIGINT DEFAULT 3 NOT NULL
@

ALTER TABLE morpheus_runs ADD COLUMN memories_scanned    BIGINT DEFAULT 0 NOT NULL
@
ALTER TABLE morpheus_runs ADD COLUMN clusters_found      BIGINT DEFAULT 0 NOT NULL
@
ALTER TABLE morpheus_runs ADD COLUMN summaries_created   BIGINT DEFAULT 0 NOT NULL
@

ALTER TABLE morpheus_runs ADD COLUMN memories_consolidated BIGINT DEFAULT 0 NOT NULL
@
ALTER TABLE morpheus_runs ADD COLUMN clusters_consolidated BIGINT DEFAULT 0 NOT NULL
@

ALTER TABLE morpheus_runs ADD COLUMN triples_extracted               BIGINT DEFAULT 0 NOT NULL
@
ALTER TABLE morpheus_runs ADD COLUMN memories_processed_for_extraction BIGINT DEFAULT 0 NOT NULL
@

-- Canonical shape: CLOB for ``config`` (Db2 has no native JSON type in
-- ORA-compat mode; we store JSON text, parse in the repository layer).
ALTER TABLE morpheus_runs ADD COLUMN config CLOB(1M)
@
ALTER TABLE morpheus_runs ADD COLUMN namespace VARCHAR(256)
@

-- ── 2. drop legacy ``run_type`` + ``metrics`` columns (Db2 12.1
--       supports DROP COLUMN). Greps confirm neither column is read by
--       any production path.
ALTER TABLE morpheus_runs DROP COLUMN run_type
@
ALTER TABLE morpheus_runs DROP COLUMN metrics
@

-- ── 3. align status default to 'running' (Postgres canonical).
ALTER TABLE morpheus_runs ALTER COLUMN status SET DEFAULT 'running'
@

-- ── 4. CHECK constraints. Wrap the constraint drop in a procedural
--       block so the migration is idempotent — Db2 lacks a native
--       ``DROP CONSTRAINT IF EXISTS`` and the migration applier only
--       swallows SQLSTATE 42710 / 42P07 / 42701, not SQLSTATE 42704
--       (undefined object).
BEGIN ATOMIC
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42704'
        BEGIN END;
    EXECUTE IMMEDIATE 'ALTER TABLE morpheus_runs DROP CONSTRAINT morpheus_runs_status_check';
END
@

ALTER TABLE morpheus_runs ADD CONSTRAINT morpheus_runs_status_check
    CHECK (status IN ('running','success','failed','rolled_back'))
@

BEGIN ATOMIC
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42704'
        BEGIN END;
    EXECUTE IMMEDIATE 'ALTER TABLE morpheus_runs DROP CONSTRAINT morpheus_runs_triggered_by_check';
END
@

ALTER TABLE morpheus_runs ADD CONSTRAINT morpheus_runs_triggered_by_check
    CHECK (triggered_by IN ('cron','manual','api'))
@

-- ── 5. indexes — Postgres ships
--         idx_morpheus_runs_status  ON morpheus_runs(status)
--         idx_morpheus_runs_started ON morpheus_runs(started_at DESC)
--         idx_morpheus_runs_namespace ON morpheus_runs(namespace) WHERE namespace IS NOT NULL
--       Db2 already has idx_morpheus_runs_status + idx_morpheus_runs_started
--       from the original 0009_morpheus_runs.sql; just add the namespace one.
CREATE INDEX idx_morpheus_runs_namespace ON morpheus_runs (namespace)
@
