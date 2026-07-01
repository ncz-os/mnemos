--#SET TERMINATOR @
-- migration: 0041_knemon_tier_assignments
-- target:    IBM Db2 12.1.5
-- purpose:   KNEMON Phase 3 tier-split table. Db2 parity port of Oracle 0041.

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE knemon_tier_assignments (
      id               BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
      task_kind        VARCHAR(256)  NOT NULL,
      tier             VARCHAR(4)    NOT NULL,
      events_total     INTEGER       NOT NULL,
      sessions_total   INTEGER       NOT NULL,
      events_per_day   DECIMAL(9,3)  NOT NULL,
      p95_latency_ms   INTEGER       NOT NULL,
      avg_latency_ms   INTEGER       NOT NULL,
      iteration        INTEGER       DEFAULT 0 NOT NULL,
      last_updated     TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
      CONSTRAINT pk_knemon_tier_assignments PRIMARY KEY (id),
      CONSTRAINT uq_knemon_tier_task_kind UNIQUE (task_kind),
      CONSTRAINT ck_knemon_tier_valid CHECK (tier IN (''B1'',''B2'',''C1'',''C2''))
    )';
END@
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END; EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_tier_tier ON knemon_tier_assignments(tier)'; END@
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END; EXECUTE IMMEDIATE 'CREATE INDEX ix_knemon_tier_iter ON knemon_tier_assignments(iteration)'; END@
