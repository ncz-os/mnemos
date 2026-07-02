-- migration: 0032_usage_ledger
-- target:    IBM Db2 12.1.x
-- purpose:   KNEMON MVP Step 1 — token/cost usage ledger.
-- design:    /Users/jasonperlow/knemon-design-draft.md section 1
-- Mirrors db/migrations_oracle/0032_usage_ledger.sql.

CREATE TABLE usage_ledger (
  id                 BIGINT        GENERATED ALWAYS AS IDENTITY NOT NULL,
  provider           VARCHAR(128)  NOT NULL,
  model              VARCHAR(256)  NOT NULL,
  task_kind          VARCHAR(64)   NOT NULL,
  tokens_in          INTEGER       NOT NULL,
  tokens_out         INTEGER       NOT NULL,
  tokens_reasoning   INTEGER       NOT NULL WITH DEFAULT 0,
  est_cost_usd       DECIMAL(12,6) NOT NULL,
  latency_ms         INTEGER       NOT NULL,
  outcome            VARCHAR(16)   NOT NULL,
  caller_subsystem   VARCHAR(128)  NOT NULL,
  tier               VARCHAR(32)   NOT NULL,
  ts                 TIMESTAMP     NOT NULL WITH DEFAULT CURRENT TIMESTAMP,
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
);

CREATE INDEX usage_ledger_ts_idx ON usage_ledger(ts);
CREATE INDEX usage_ledger_model_idx ON usage_ledger(provider, model);
