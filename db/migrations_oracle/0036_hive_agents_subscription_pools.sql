-- migration: 0036_hive_agents_subscription_pools
-- target:    Oracle 23ai PDB ORCLPDB1 (PYTHIA + CERBERUS standby)
-- purpose:   Advertise workspace-local subscription pools per hive agent.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM user_tab_columns
  WHERE table_name = 'HIVE_AGENTS'
    AND column_name = 'SUBSCRIPTION_POOLS';

  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      ALTER TABLE hive_agents
      ADD subscription_pools CLOB CHECK (subscription_pools IS JSON)
    ]';
  END IF;
END;
/

COMMIT;
