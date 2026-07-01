-- migration: 0042_knemon_baseline_tables
-- target:    Oracle 23ai PDB ORCLPDB1
-- purpose:   KNEMON Phase 1 — baseline snapshot table (event-level 48h window)
--            plus knemon_baselines registry for snapshot metadata.
-- design:    knemon_phase1_baseline_2026_05_28 holds raw per-event
--            plan-status + model-routing rows sourced from usage_ledger.
--            knemon_baselines registers each snapshot run for auditability
--            and downstream gate checks.
-- Idempotency: guarded by USER_TABLES.

-- ── Phase 1 baseline snapshot table ─────────────────────────────────────

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables
   WHERE table_name = 'KNEMON_PHASE1_BASELINE_2026_05_28';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE knemon_phase1_baseline_2026_05_28 (
        event_id        NUMBER(12)     NOT NULL,
        session_urn     VARCHAR2(256),
        plan_window_id  VARCHAR2(128),
        task_kind       VARCHAR2(128)  NOT NULL,
        provider        VARCHAR2(128)  NOT NULL,
        model           VARCHAR2(256)  NOT NULL,
        tokens_in       NUMBER(12)     NOT NULL,
        tokens_out      NUMBER(12)     NOT NULL,
        cost_usd        NUMBER(14,8)   NOT NULL,
        ts_utc          TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT pk_knemon_phase1_20260528 PRIMARY KEY (event_id)
      )
    ]';
    EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_task_kind ON knemon_phase1_baseline_2026_05_28(task_kind)';
    EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_provider   ON knemon_phase1_baseline_2026_05_28(provider)';
    EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_model      ON knemon_phase1_baseline_2026_05_28(provider, model)';
    EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_session    ON knemon_phase1_baseline_2026_05_28(session_urn)';
    EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_ts         ON knemon_phase1_baseline_2026_05_28(ts_utc)';
  END IF;
END;
/

-- ── Baseline registry table ─────────────────────────────────────────────

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables
   WHERE table_name = 'KNEMON_BASELINES';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE knemon_baselines (
        id              NUMBER GENERATED ALWAYS AS IDENTITY,
        baseline_name   VARCHAR2(128)  NOT NULL,
        table_name      VARCHAR2(128)  NOT NULL,
        window_start    TIMESTAMP WITH TIME ZONE NOT NULL,
        window_end      TIMESTAMP WITH TIME ZONE NOT NULL,
        event_count     NUMBER(12)     NOT NULL,
        session_count   NUMBER(9)      NOT NULL,
        task_kind_count NUMBER(9)      NOT NULL,
        source_table    VARCHAR2(128)  NOT NULL,
        created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        notes           VARCHAR2(1024),
        CONSTRAINT pk_knemon_baselines PRIMARY KEY (id),
        CONSTRAINT uq_knemon_baseline_name UNIQUE (baseline_name)
      )
    ]';
  END IF;
END;
/

COMMIT;
