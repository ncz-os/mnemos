-- migration: 0036_hive_agents_subscription_pools
-- target:    IBM Db2 12.1.5 (Oracle Compat mode)
-- purpose:   Advertise workspace-local subscription pools per hive agent.

BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM syscat.columns
    WHERE tabschema = CURRENT SCHEMA
      AND tabname = 'HIVE_AGENTS'
      AND colname = 'SUBSCRIPTION_POOLS'
  ) THEN
    EXECUTE IMMEDIATE '
      ALTER TABLE hive_agents
      ADD COLUMN subscription_pools CLOB(1M) INLINE LENGTH 4096
    ';
  END IF;
END%
