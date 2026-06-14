-- migration: 0042_knemon_baseline_tables
-- target:    PostgreSQL 16
-- purpose:   KNEMON Phase 1 baseline snapshot + registry. PostgreSQL parity
--            port of db/migrations_oracle/0042_knemon_baseline_tables.sql.

CREATE TABLE IF NOT EXISTS knemon_phase1_baseline_2026_05_28 (
    event_id        BIGINT        NOT NULL,
    session_urn     VARCHAR(256),
    plan_window_id  VARCHAR(128),
    task_kind       VARCHAR(128)  NOT NULL,
    provider        VARCHAR(128)  NOT NULL,
    model           VARCHAR(256)  NOT NULL,
    tokens_in       INTEGER       NOT NULL,
    tokens_out      INTEGER       NOT NULL,
    cost_usd        NUMERIC(14,8) NOT NULL,
    ts_utc          TIMESTAMPTZ   NOT NULL,
    CONSTRAINT pk_knemon_phase1_20260528 PRIMARY KEY (event_id)
);

CREATE INDEX IF NOT EXISTS ix_knemon_p1b_task_kind ON knemon_phase1_baseline_2026_05_28(task_kind);
CREATE INDEX IF NOT EXISTS ix_knemon_p1b_provider   ON knemon_phase1_baseline_2026_05_28(provider);
CREATE INDEX IF NOT EXISTS ix_knemon_p1b_model      ON knemon_phase1_baseline_2026_05_28(provider, model);
CREATE INDEX IF NOT EXISTS ix_knemon_p1b_session    ON knemon_phase1_baseline_2026_05_28(session_urn);
CREATE INDEX IF NOT EXISTS ix_knemon_p1b_ts         ON knemon_phase1_baseline_2026_05_28(ts_utc);

CREATE TABLE IF NOT EXISTS knemon_baselines (
    id              BIGSERIAL PRIMARY KEY,
    baseline_name   VARCHAR(128)  NOT NULL,
    table_name      VARCHAR(128)  NOT NULL,
    window_start    TIMESTAMPTZ   NOT NULL,
    window_end      TIMESTAMPTZ   NOT NULL,
    event_count     INTEGER       NOT NULL,
    session_count   INTEGER       NOT NULL,
    task_kind_count INTEGER       NOT NULL,
    source_table    VARCHAR(128)  NOT NULL,
    created_at      TIMESTAMPTZ   DEFAULT NOW() NOT NULL,
    notes           TEXT,
    CONSTRAINT uq_knemon_baseline_name UNIQUE (baseline_name)
);
