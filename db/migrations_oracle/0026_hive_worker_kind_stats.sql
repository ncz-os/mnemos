-- migration: 0026_hive_worker_kind_stats
-- target:    Oracle 23ai PDB ORCLPDB1
-- schema:    HIVE_MIND
-- purpose:   Per-worker per-kind aggregate counters. Used by dispatcher to
--            steer work toward agents with the best track record on a given
--            kind. Mirrors /srv/agent-bus/agents.db `worker_kind_stats`.
--
-- Notes:
--   - Composite PK (urn, kind). One row per (worker, kind) pair.
--   - All counts BIGINT-equivalent so a long-lived worker never overflows.
--   - total_cost_usd tallies real spend (matches estimated_cost_usd on jobs).
--   - last_run tracked for staleness pruning.
--
-- Upsert pattern: MERGE INTO ... WHEN MATCHED THEN UPDATE SET counts +=
--                 incoming; WHEN NOT MATCHED THEN INSERT.
--
-- Idempotency + drift reconciliation: an earlier migration
-- (0011_hive_mind_extended_columns) also creates HIVE_WORKER_KIND_STATS
-- with a now-superseded shape (agent_urn / claims / completions /
-- last_seen_at). On a clean deploy 0011 runs first and wins the CREATE,
-- so this migration (1) creates the canonical table only when absent,
-- (2) reconciles missing columns onto a pre-existing table, and
-- (3) builds each index only when its drift-prone target column exists.

-- (1) Create the canonical table only when none exists yet.
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'HIVE_WORKER_KIND_STATS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hive_worker_kind_stats (
        urn                  VARCHAR2(256)  NOT NULL,
        kind                 VARCHAR2(256)  NOT NULL,
        success_count        NUMBER(15)     DEFAULT 0 NOT NULL,
        fail_count           NUMBER(15)     DEFAULT 0 NOT NULL,
        cancelled_count      NUMBER(15)     DEFAULT 0 NOT NULL,
        total_tokens_in      NUMBER(15)     DEFAULT 0 NOT NULL,
        total_tokens_out     NUMBER(15)     DEFAULT 0 NOT NULL,
        total_cost_usd       NUMBER(15, 6)  DEFAULT 0 NOT NULL,
        total_duration_sec   NUMBER(15, 3)  DEFAULT 0 NOT NULL,
        last_run             NUMBER,
        CONSTRAINT pk_hive_wkstats PRIMARY KEY (urn, kind)
      )
    ]';
  END IF;
END;
/

-- (2) Reconcile column drift: add canonical columns the 0011 shape lacks.
-- Added nullable so the ALTER is safe regardless of existing rows; this
-- is dispatcher telemetry with no ORM consumer in mnemos.
DECLARE
  v_count NUMBER;
  PROCEDURE add_col(p_col VARCHAR2, p_ddl VARCHAR2) IS
  BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tab_columns
     WHERE table_name = 'HIVE_WORKER_KIND_STATS' AND column_name = UPPER(p_col);
    IF v_count = 0 THEN
      EXECUTE IMMEDIATE 'ALTER TABLE hive_worker_kind_stats ADD (' || p_ddl || ')';
    END IF;
  END;
BEGIN
  add_col('urn',              'urn VARCHAR2(256)');
  add_col('success_count',    'success_count NUMBER(15) DEFAULT 0');
  add_col('fail_count',       'fail_count NUMBER(15) DEFAULT 0');
  add_col('cancelled_count',  'cancelled_count NUMBER(15) DEFAULT 0');
  add_col('total_tokens_in',  'total_tokens_in NUMBER(15) DEFAULT 0');
  add_col('total_tokens_out', 'total_tokens_out NUMBER(15) DEFAULT 0');
  add_col('total_cost_usd',   'total_cost_usd NUMBER(15,6) DEFAULT 0');
  add_col('last_run',         'last_run NUMBER');
END;
/

-- (3) Indexes: create only when the index is absent AND its drift-prone
-- target column exists (kind is present in both shapes, so guarding on
-- last_run is sufficient to avoid ORA-00904).
DECLARE
  PROCEDURE create_index(p_name VARCHAR2, p_col VARCHAR2, p_ddl VARCHAR2) IS
    v_i NUMBER;
    v_c NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_i FROM user_indexes WHERE index_name = p_name;
    SELECT COUNT(*) INTO v_c FROM user_tab_columns
     WHERE table_name = 'HIVE_WORKER_KIND_STATS' AND column_name = UPPER(p_col);
    IF v_i = 0 AND v_c > 0 THEN EXECUTE IMMEDIATE p_ddl; END IF;
  END;
BEGIN
  create_index('IX_HIVE_WKSTATS_KIND', 'kind',
               'CREATE INDEX ix_hive_wkstats_kind ON hive_worker_kind_stats(kind)');
  create_index('IX_HIVE_WKSTATS_LAST_RUN', 'last_run',
               'CREATE INDEX ix_hive_wkstats_last_run ON hive_worker_kind_stats(last_run)');
END;
/

COMMIT;
