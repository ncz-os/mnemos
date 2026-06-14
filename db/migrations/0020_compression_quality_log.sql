-- 0020_compression_quality_log.sql — PostgreSQL migration-parity shim.
-- PostgreSQL already carries this historical schema change via the legacy flat
-- migrations at db/migrations*.sql or the canonical later numbered migration.
-- This file exists so scripts/check_migration_parity.py --mode full can gate
-- that pg/oracle/db2 carry the same migration basenames. Idempotent no-op.

DO $$
BEGIN
    NULL;
END
$$;
