-- migration: 0032_usage_ledger
-- target:    PostgreSQL 16
-- purpose:   KNEMON MVP Step 1 - token/cost usage ledger.
-- design:    /Users/jasonperlow/knemon-design-draft.md section 1

CREATE TABLE IF NOT EXISTS usage_ledger (
  id                 BIGSERIAL PRIMARY KEY,
  provider           TEXT        NOT NULL,
  model              TEXT        NOT NULL,
  task_kind          TEXT        NOT NULL,
  tokens_in          INT         NOT NULL,
  tokens_out         INT         NOT NULL,
  tokens_reasoning   INT         NOT NULL DEFAULT 0,
  est_cost_usd       NUMERIC(12,6) NOT NULL,
  latency_ms         INT         NOT NULL,
  outcome            TEXT        NOT NULL,
  caller_subsystem   TEXT        NOT NULL,
  tier               TEXT        NOT NULL,
  ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_usage_ledger_tokens_in_nonnegative
    CHECK (tokens_in >= 0),
  CONSTRAINT ck_usage_ledger_tokens_out_nonnegative
    CHECK (tokens_out >= 0),
  CONSTRAINT ck_usage_ledger_tokens_reasoning_nonnegative
    CHECK (tokens_reasoning >= 0),
  CONSTRAINT ck_usage_ledger_est_cost_usd_nonnegative
    CHECK (est_cost_usd >= 0),
  CONSTRAINT ck_usage_ledger_latency_ms_nonnegative
    CHECK (latency_ms >= 0),
  CONSTRAINT ck_usage_ledger_outcome
    CHECK (outcome IN ('ok','err','timeout'))
);

CREATE INDEX IF NOT EXISTS usage_ledger_ts_idx ON usage_ledger(ts);
CREATE INDEX IF NOT EXISTS usage_ledger_model_idx ON usage_ledger(provider, model);

GRANT SELECT, INSERT ON usage_ledger TO mnemos_user;
GRANT USAGE, SELECT ON SEQUENCE usage_ledger_id_seq TO mnemos_user;
GRANT SELECT, INSERT ON usage_ledger TO mnemos;
GRANT USAGE, SELECT ON SEQUENCE usage_ledger_id_seq TO mnemos;
