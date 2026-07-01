-- migration: 0032_usage_ledger
-- target:    Oracle 23ai PDB ORCLPDB1
-- purpose:   KNEMON MVP Step 1 — token/cost usage ledger.
-- design:    /Users/jasonperlow/knemon-design-draft.md section 1
-- Mirrors db/migrations/0032_usage_ledger.sql (PostgreSQL).
-- Idempotency: guarded by USER_TABLES.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'USAGE_LEDGER';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE usage_ledger (
        id                 NUMBER GENERATED ALWAYS AS IDENTITY,
        provider           VARCHAR2(128)  NOT NULL,
        model              VARCHAR2(256)  NOT NULL,
        task_kind          VARCHAR2(64)   NOT NULL,
        tokens_in          NUMBER(12)     NOT NULL,
        tokens_out         NUMBER(12)     NOT NULL,
        tokens_reasoning   NUMBER(12)     DEFAULT 0 NOT NULL,
        est_cost_usd       NUMBER(12,6)   NOT NULL,
        latency_ms         NUMBER(12)     NOT NULL,
        outcome            VARCHAR2(16)   NOT NULL,
        caller_subsystem   VARCHAR2(128)  NOT NULL,
        tier               VARCHAR2(32)   NOT NULL,
        ts                 TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
        CONSTRAINT pk_usage_ledger PRIMARY KEY (id),
        CONSTRAINT ck_usage_ledger_tokens_in_nonneg
          CHECK (tokens_in >= 0),
        CONSTRAINT ck_usage_ledger_tokens_out_nonneg
          CHECK (tokens_out >= 0),
        CONSTRAINT ck_usage_ledger_tokens_reasoning_nonneg
          CHECK (tokens_reasoning >= 0),
        CONSTRAINT ck_usage_ledger_est_cost_usd_nonneg
          CHECK (est_cost_usd >= 0),
        CONSTRAINT ck_usage_ledger_latency_ms_nonneg
          CHECK (latency_ms >= 0),
        CONSTRAINT ck_usage_ledger_outcome
          CHECK (outcome IN ('ok','err','timeout'))
      )
    ]';
    EXECUTE IMMEDIATE 'CREATE INDEX usage_ledger_ts_idx ON usage_ledger(ts)';
    EXECUTE IMMEDIATE 'CREATE INDEX usage_ledger_model_idx ON usage_ledger(provider, model)';
  END IF;
END;
/
