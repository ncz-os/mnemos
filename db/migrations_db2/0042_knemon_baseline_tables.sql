--#SET TERMINATOR %
-- migration: 0042_knemon_baseline_tables
-- target:    IBM Db2 12.1.5
-- purpose:   KNEMON Phase 1 baseline snapshot + registry. Db2 parity port of Oracle 0042.

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE knemon_phase1_baseline_2026_05_28 (
      event_id        BIGINT        NOT NULL,
      session_urn     VARCHAR(256),
      plan_window_id  VARCHAR(128),
      task_kind       VARCHAR(128)  NOT NULL,
      provider        VARCHAR(128)  NOT NULL,
      model           VARCHAR(256)  NOT NULL,
      tokens_in       INTEGER       NOT NULL,
      tokens_out      INTEGER       NOT NULL,
      cost_usd        DECIMAL(14,8) NOT NULL,
      ts_utc          TIMESTAMP(6) WITH TIME ZONE NOT NULL,
      CONSTRAINT pk_knemon_phase1_20260528 PRIMARY KEY (event_id)
    )';
END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END; EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_task_kind ON knemon_phase1_baseline_2026_05_28(task_kind)'; END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END; EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_provider ON knemon_phase1_baseline_2026_05_28(provider)'; END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END; EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_model ON knemon_phase1_baseline_2026_05_28(provider, model)'; END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END; EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_session ON knemon_phase1_baseline_2026_05_28(session_urn)'; END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END; EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_p1b_ts ON knemon_phase1_baseline_2026_05_28(ts_utc)'; END%

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE knemon_baselines (
      id              BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
      baseline_name   VARCHAR(128)  NOT NULL,
      table_name      VARCHAR(128)  NOT NULL,
      window_start    TIMESTAMP(6) WITH TIME ZONE NOT NULL,
      window_end      TIMESTAMP(6) WITH TIME ZONE NOT NULL,
      event_count     INTEGER       NOT NULL,
      session_count   INTEGER       NOT NULL,
      task_kind_count INTEGER       NOT NULL,
      source_table    VARCHAR(128)  NOT NULL,
      created_at      TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
      notes           VARCHAR(1024),
      CONSTRAINT pk_knemon_baselines PRIMARY KEY (id),
      CONSTRAINT uq_knemon_baseline_name UNIQUE (baseline_name)
    )';
END%
