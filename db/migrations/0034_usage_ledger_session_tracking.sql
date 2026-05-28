-- migration: 0034_usage_ledger_session_tracking
-- PostgreSQL mirror of Oracle 0034_usage_ledger_session_tracking.

ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS request_count NUMERIC NOT NULL DEFAULT 1;
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS plan_window_id TEXT;
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS subscription_amortized BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS usage_ledger_session_idx ON usage_ledger(session_id);
CREATE INDEX IF NOT EXISTS usage_ledger_window_idx ON usage_ledger(plan_window_id);
