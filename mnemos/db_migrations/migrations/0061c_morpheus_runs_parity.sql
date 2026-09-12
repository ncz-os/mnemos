-- 0061c_morpheus_runs_parity.sql — PostgreSQL numbered parity anchor.
-- Postgres's morpheus_runs is already canonical (created by
-- db/migrations_v3_3_morpheus.sql + its 3 follow-on ALTER files); this
-- item (11a) only needed to retcon the divergent Oracle/DB2/SQLite/
-- MySQL/MariaDB shapes to match it. No Postgres schema change needed.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
