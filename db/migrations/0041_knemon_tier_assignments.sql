-- 0041_knemon_tier_assignments.sql — PostgreSQL migration-parity shim.
-- PostgreSQL KNEMON tables are operational artifacts from the Oracle rollout;
-- no canonical PG installer path consumes them today. Keep the basename present
-- so full migration parity stays enforceable across pg/oracle/db2.

DO $$
BEGIN
    NULL;
END
$$;
