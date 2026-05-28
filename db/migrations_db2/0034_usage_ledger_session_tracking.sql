-- migration: 0034_usage_ledger_session_tracking
-- Db2 mirror of Oracle 0034_usage_ledger_session_tracking.

ALTER TABLE usage_ledger ADD COLUMN session_id VARCHAR(64);
ALTER TABLE usage_ledger ADD COLUMN request_count DECIMAL(20,0) NOT NULL DEFAULT 1;
ALTER TABLE usage_ledger ADD COLUMN plan_window_id VARCHAR(64);
ALTER TABLE usage_ledger ADD COLUMN subscription_amortized SMALLINT NOT NULL DEFAULT 0;

CREATE INDEX usage_ledger_session_idx ON usage_ledger(session_id);
CREATE INDEX usage_ledger_window_idx ON usage_ledger(plan_window_id);
