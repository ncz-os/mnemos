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
-- Idempotency: guarded by USER_TABLES.

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

DECLARE
  PROCEDURE create_index(p_name VARCHAR2, p_ddl VARCHAR2) IS
    v_n NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_n FROM user_indexes WHERE index_name = p_name;
    IF v_n = 0 THEN EXECUTE IMMEDIATE p_ddl; END IF;
  END;
BEGIN
  create_index('IX_HIVE_WKSTATS_KIND',
               'CREATE INDEX ix_hive_wkstats_kind ON hive_worker_kind_stats(kind)');
  create_index('IX_HIVE_WKSTATS_LAST_RUN',
               'CREATE INDEX ix_hive_wkstats_last_run ON hive_worker_kind_stats(last_run)');
END;
/

COMMIT;
