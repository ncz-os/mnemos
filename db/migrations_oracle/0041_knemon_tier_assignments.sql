-- migration: 0041_knemon_tier_assignments
-- target:    Oracle 23ai PDB ORCLPDB1
-- purpose:   KNEMON Phase 3 — tier-split table (B1/B2/C1/C2) from
--            knemon_phase1_baseline_2026_05_28 throughput + latency data.
-- design:    Tier = throughput_class (B=high >1K/day, C=low <=1K/day)
--            × latency_class (1=fast <5s p95, 2=slow >=5s p95).
--            "Iterate-in-place" per directive 7: the executable script
--            INSERTs initial assignments then iteratively refines the
--            tier column in-place via successive UPDATE passes until
--            convergence (no rows change tier).
-- Idempotency: guarded by USER_TABLES.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'KNEMON_TIER_ASSIGNMENTS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE knemon_tier_assignments (
        id               NUMBER GENERATED ALWAYS AS IDENTITY,
        task_kind        VARCHAR2(256)  NOT NULL,
        tier             VARCHAR2(4)    NOT NULL,
        events_total     NUMBER(12)     NOT NULL,
        sessions_total   NUMBER(9)      NOT NULL,
        events_per_day   NUMBER(9, 3)   NOT NULL,
        p95_latency_ms   NUMBER(12)     NOT NULL,
        avg_latency_ms   NUMBER(12)     NOT NULL,
        iteration        NUMBER(5)      DEFAULT 0 NOT NULL,
        last_updated     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        CONSTRAINT pk_knemon_tier_assignments PRIMARY KEY (id),
        CONSTRAINT uq_knemon_tier_task_kind UNIQUE (task_kind),
        CONSTRAINT ck_knemon_tier_valid
          CHECK (tier IN ('B1','B2','C1','C2'))
      )
    ]';
    EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_tier_tier ON knemon_tier_assignments(tier)';
    EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_tier_iter ON knemon_tier_assignments(iteration)';
  END IF;
END;
/

COMMIT;
