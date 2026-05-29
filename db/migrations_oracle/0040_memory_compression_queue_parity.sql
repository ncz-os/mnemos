-- migration: 0040_memory_compression_queue_parity
-- target:    Oracle 23ai PDB ORCLPDB1
-- purpose:   GAP 1 (job 019e7049, CHILD A) — bring the Oracle
--            memory_compression_queue to FULL PARITY with the canonical
--            Postgres schema (db/migrations_v3_1_compression.sql) so the
--            distillation/compression contest runs identically on every
--            backend behind the persistence ABC. The original Oracle
--            0017_memory_compression_queue.sql shipped a minimal stub
--            (id, memory_id, priority, queued_at, processed_at, status)
--            that is MISSING the columns the contest worker requires:
--            owner_id, reason, scoring_profile, attempts, enqueued_at,
--            started_at, finished_at, error. The stub was never applied to
--            live Oracle (table absent → ORA-00942), so the queue + admin
--            enqueue routes 503/404 there.
-- design:    GRAEAE consult 1c3e8a7f (athena/hephaestus/metis).
--            Architectural law mem_1780005765033 — identical schema +
--            feature set on every hive backend through the ABC.
--
-- Notes:
--   - id mirrors PG's gen_random_uuid() DEFAULT: a DB-side default
--     (LOWER(RAWTOHEX(SYS_GUID())) → 32 hex chars) so the INSERT omits id
--     on every backend, exactly as the Postgres enqueue does.
--   - error is VARCHAR2(4000): contest breadcrumbs ('infra_retry:...',
--     'stranded_running:...') are short and the sweep uses LIKE on it.
--   - Oracle has no partial indexes; idx_mcq_ready is a plain composite
--     over (status, priority DESC, enqueued_at).
--
-- Idempotency + reconciliation: guarded by USER_TABLES + USER_TAB_COLUMNS.
--   * table absent           → CREATE full-parity table.
--   * table present, no
--     owner_id column (stub)  → DROP + CREATE full-parity (the queue is a
--                               transient work table; dropping pending rows
--                               is safe — they are re-enqueued on demand).
--   * table present WITH
--     owner_id                → no-op (already parity).

DECLARE
  v_tbl     NUMBER;
  v_col     NUMBER;
  v_narrow  NUMBER;

  PROCEDURE create_table IS
  BEGIN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE memory_compression_queue (
        id               VARCHAR2(36)             DEFAULT LOWER(RAWTOHEX(SYS_GUID())) NOT NULL,
        memory_id        VARCHAR2(100)            NOT NULL,
        owner_id         VARCHAR2(255)            DEFAULT 'default'  NOT NULL,
        reason           VARCHAR2(32)             NOT NULL,
        status           VARCHAR2(16)             DEFAULT 'pending'  NOT NULL,
        priority         NUMBER(5)                DEFAULT 0          NOT NULL,
        scoring_profile  VARCHAR2(32)             DEFAULT 'balanced' NOT NULL,
        attempts         NUMBER(5)                DEFAULT 0          NOT NULL,
        enqueued_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        started_at       TIMESTAMP WITH TIME ZONE,
        finished_at      TIMESTAMP WITH TIME ZONE,
        error            VARCHAR2(4000),
        CONSTRAINT mcq_pk PRIMARY KEY (id),
        CONSTRAINT mcq_memory_fk FOREIGN KEY (memory_id)
          REFERENCES memories(id) ON DELETE CASCADE,
        CONSTRAINT mcq_status_valid
          CHECK (status IN ('pending','running','done','failed')),
        CONSTRAINT mcq_reason_valid
          CHECK (reason IN ('on_write','manual','scheduled','reprocess')),
        CONSTRAINT mcq_scoring_profile_valid
          CHECK (scoring_profile IN ('balanced','quality_first','speed_first','custom'))
      )
    ]';
    EXECUTE IMMEDIATE
      'CREATE INDEX idx_mcq_ready ON memory_compression_queue '
      || '(status, priority DESC, enqueued_at)';
    EXECUTE IMMEDIATE
      'CREATE INDEX idx_mcq_memory ON memory_compression_queue (memory_id)';
    EXECUTE IMMEDIATE
      'CREATE INDEX idx_mcq_owner ON memory_compression_queue (owner_id)';
  END;
BEGIN
  SELECT COUNT(*) INTO v_tbl FROM user_tables
   WHERE table_name = 'MEMORY_COMPRESSION_QUEUE';

  IF v_tbl = 0 THEN
    create_table;
  ELSE
    SELECT COUNT(*) INTO v_col FROM user_tab_columns
     WHERE table_name = 'MEMORY_COMPRESSION_QUEUE'
       AND column_name = 'OWNER_ID';
    IF v_col = 0 THEN
      -- pre-parity stub: drop transient work table + recreate at parity.
      EXECUTE IMMEDIATE 'DROP TABLE memory_compression_queue CASCADE CONSTRAINTS';
      create_table;
    ELSE
      -- owner_id present but a prior 0040 may have created memory_id too
      -- narrow (36); real ids (mnemos_<sha32>, 39 chars) overflow it.
      -- Widen in place — idempotent (no-op once already 100).
      SELECT COUNT(*) INTO v_narrow FROM user_tab_columns
       WHERE table_name = 'MEMORY_COMPRESSION_QUEUE'
         AND column_name = 'MEMORY_ID'
         AND char_length < 100;
      IF v_narrow > 0 THEN
        EXECUTE IMMEDIATE
          'ALTER TABLE memory_compression_queue MODIFY (memory_id VARCHAR2(100))';
      END IF;
    END IF;
  END IF;
END;
/
