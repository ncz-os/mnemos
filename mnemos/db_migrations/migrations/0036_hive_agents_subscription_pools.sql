-- migration: 0036_hive_agents_subscription_pools
-- target:    PostgreSQL 16 + pgvector (development + cixmini edge)
-- purpose:   Advertise workspace-local subscription pools per hive agent.

ALTER TABLE hive_agents
  ADD COLUMN IF NOT EXISTS subscription_pools JSONB;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'ck_hive_agents_subscription_pools_json_array'
  ) THEN
    ALTER TABLE hive_agents
      ADD CONSTRAINT ck_hive_agents_subscription_pools_json_array
      CHECK (subscription_pools IS NULL OR jsonb_typeof(subscription_pools) = 'array');
  END IF;
END $$;
