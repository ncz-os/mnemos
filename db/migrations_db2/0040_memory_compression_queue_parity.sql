-- migration: 0040_memory_compression_queue_parity
-- target:    IBM Db2 12.1.5 (Oracle Compat mode) — GA 2026-06-06
-- purpose:   GAP 1 (job 019e7049, CHILD A) — bring the Db2
--            memory_compression_queue to FULL PARITY with the canonical
--            Postgres schema (db/migrations/0040). The original Db2
--            0017_memory_compression_queue.sql shipped a minimal stub
--            (id, memory_id, priority, queued_at, processed_at, status)
--            missing the contest columns owner_id, reason, scoring_profile,
--            attempts, enqueued_at, started_at, finished_at, error.
-- design:    Mirrors db/migrations_oracle/0040 + db/migrations/0040.
--            Architectural law mem_1780005765033 — identical schema +
--            feature set on every hive backend behind the persistence ABC.
--
-- Statement terminator is ``%`` (Db2 compound-SQL convention used across
-- db/migrations_db2/; see 0021_hive_agents.sql). SQLSTATE 42710 =
-- "object already exists" → swallowed for idempotent CREATE.
--
-- CHILD D (019e7106-d751) owns the Db2 native CompressionQueueRepository
-- impl + live SKIP LOCKED DATA validation once Db2 12.1.5 is GA; this
-- migration only establishes the parity schema it targets.

-- Drop ONLY the pre-parity stub (table present but lacking owner_id).
-- Idempotent: no-op on a fresh DB (table absent) and on an already-parity
-- table (owner_id present). Never drops a populated parity queue.
BEGIN
  DECLARE v_stub INTEGER DEFAULT 0;
  DECLARE CONTINUE HANDLER FOR SQLEXCEPTION BEGIN END;
  SELECT COUNT(*) INTO v_stub FROM SYSCAT.TABLES t
    WHERE t.TABNAME = 'MEMORY_COMPRESSION_QUEUE'
      AND t.TABSCHEMA = CURRENT SCHEMA
      AND NOT EXISTS (
        SELECT 1 FROM SYSCAT.COLUMNS c
        WHERE c.TABNAME = t.TABNAME
          AND c.TABSCHEMA = t.TABSCHEMA
          AND c.COLNAME = 'OWNER_ID');
  IF v_stub > 0 THEN
    EXECUTE IMMEDIATE 'DROP TABLE memory_compression_queue';
  END IF;
END%

-- Full-parity table. id has no column DEFAULT (Db2 forbids non-deterministic
-- functions like GENERATE_UNIQUE in DEFAULT); the BEFORE INSERT trigger below
-- supplies it so the ABC enqueue path omits id on every backend.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE memory_compression_queue (
      id               VARCHAR(36)                 NOT NULL,
      memory_id        VARCHAR(100)                NOT NULL,
      owner_id         VARCHAR(255)                NOT NULL DEFAULT ''default'',
      reason           VARCHAR(32)                 NOT NULL,
      status           VARCHAR(16)                 NOT NULL DEFAULT ''pending'',
      priority         SMALLINT                    NOT NULL DEFAULT 0,
      scoring_profile  VARCHAR(32)                 NOT NULL DEFAULT ''balanced'',
      attempts         SMALLINT                    NOT NULL DEFAULT 0,
      enqueued_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      started_at       TIMESTAMP,
      finished_at      TIMESTAMP,
      error            VARCHAR(4000),
      CONSTRAINT mcq_pk PRIMARY KEY (id),
      CONSTRAINT mcq_memory_fk FOREIGN KEY (memory_id)
        REFERENCES memories(id) ON DELETE CASCADE,
      CONSTRAINT mcq_status_valid
        CHECK (status IN (''pending'',''running'',''done'',''failed'')),
      CONSTRAINT mcq_reason_valid
        CHECK (reason IN (''on_write'',''manual'',''scheduled'',''reprocess'')),
      CONSTRAINT mcq_scoring_profile_valid
        CHECK (scoring_profile IN (''balanced'',''quality_first'',''speed_first'',''custom''))
    )';
END%

-- Reconcile a table left by a prior 0040 that created memory_id too narrow
-- (36); real ids (mnemos_<sha32>, 39 chars) overflow it. Widen in place.
-- Idempotent: no-op once already 100 (and on a fresh create above).
BEGIN
  DECLARE v_narrow INTEGER DEFAULT 0;
  DECLARE CONTINUE HANDLER FOR SQLEXCEPTION BEGIN END;
  SELECT COUNT(*) INTO v_narrow FROM SYSCAT.COLUMNS
    WHERE TABNAME = 'MEMORY_COMPRESSION_QUEUE'
      AND TABSCHEMA = CURRENT SCHEMA
      AND COLNAME = 'MEMORY_ID'
      AND LENGTH < 100;
  IF v_narrow > 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE memory_compression_queue ALTER COLUMN memory_id SET DATA TYPE VARCHAR(100)';
  END IF;
END%

-- DB-side id generation (parity with PG gen_random_uuid + Oracle SYS_GUID
-- defaults): populate id when the INSERT omits it. HEX(GENERATE_UNIQUE())
-- is 26 chars, fits VARCHAR(36).
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TRIGGER mcq_bi_id NO CASCADE BEFORE INSERT ON memory_compression_queue
    REFERENCING NEW AS n FOR EACH ROW
    WHEN (n.id IS NULL)
      SET n.id = LOWER(HEX(GENERATE_UNIQUE()))';
END%

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_mcq_ready ON memory_compression_queue (status, priority DESC, enqueued_at)';
END%
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_mcq_memory ON memory_compression_queue (memory_id)';
END%
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_mcq_owner ON memory_compression_queue (owner_id)';
END%
