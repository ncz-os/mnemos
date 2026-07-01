-- migration: 0041_knemon_tier_assignments
-- target:    PostgreSQL 16
-- purpose:   KNEMON Phase 3 tier-split table. PostgreSQL parity port of
--            db/migrations_oracle/0041_knemon_tier_assignments.sql.

CREATE TABLE IF NOT EXISTS knemon_tier_assignments (
    id               BIGSERIAL PRIMARY KEY,
    task_kind        VARCHAR(256)  NOT NULL,
    tier             VARCHAR(4)    NOT NULL,
    events_total     INTEGER       NOT NULL,
    sessions_total   INTEGER       NOT NULL,
    events_per_day   NUMERIC(9,3)  NOT NULL,
    p95_latency_ms   INTEGER       NOT NULL,
    avg_latency_ms   INTEGER       NOT NULL,
    iteration        INTEGER       DEFAULT 0 NOT NULL,
    last_updated     TIMESTAMPTZ   DEFAULT NOW() NOT NULL,
    CONSTRAINT uq_knemon_tier_task_kind UNIQUE (task_kind),
    CONSTRAINT ck_knemon_tier_valid CHECK (tier IN ('B1','B2','C1','C2'))
);

CREATE INDEX IF NOT EXISTS ix_knemon_tier_tier ON knemon_tier_assignments(tier);
CREATE INDEX IF NOT EXISTS ix_knemon_tier_iter ON knemon_tier_assignments(iteration);
