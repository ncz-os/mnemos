-- migration: 0034_usage_ledger_session_tracking
-- Adds KNEMON session/window utilization tracking columns to usage_ledger.

DECLARE
  n NUMBER;
BEGIN
  SELECT COUNT(*) INTO n FROM user_tab_columns WHERE table_name = 'USAGE_LEDGER' AND column_name = 'SESSION_ID';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE usage_ledger ADD session_id VARCHAR2(64)';
  END IF;

  SELECT COUNT(*) INTO n FROM user_tab_columns WHERE table_name = 'USAGE_LEDGER' AND column_name = 'REQUEST_COUNT';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE usage_ledger ADD request_count NUMBER DEFAULT 1 NOT NULL';
  END IF;

  SELECT COUNT(*) INTO n FROM user_tab_columns WHERE table_name = 'USAGE_LEDGER' AND column_name = 'PLAN_WINDOW_ID';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE usage_ledger ADD plan_window_id VARCHAR2(64)';
  END IF;

  SELECT COUNT(*) INTO n FROM user_tab_columns WHERE table_name = 'USAGE_LEDGER' AND column_name = 'SUBSCRIPTION_AMORTIZED';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE usage_ledger ADD subscription_amortized NUMBER(1) DEFAULT 0 NOT NULL';
  END IF;

  SELECT COUNT(*) INTO n FROM user_indexes WHERE index_name = 'USAGE_LEDGER_SESSION_IDX';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'CREATE INDEX usage_ledger_session_idx ON usage_ledger(session_id)';
  END IF;

  SELECT COUNT(*) INTO n FROM user_indexes WHERE index_name = 'USAGE_LEDGER_WINDOW_IDX';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'CREATE INDEX usage_ledger_window_idx ON usage_ledger(plan_window_id)';
  END IF;
END;
/
