-- 0001_core_schema.sql — PostgreSQL numbered parity anchor.
--
-- The PostgreSQL core schema predates the numbered backend tree and is still
-- shipped through db/migrations.sql plus the historical db/migrations_v*.sql
-- files.  This idempotent anchor gives the pg/oracle/db2 parity gate the same
-- basename inventory without replaying legacy bootstrap DDL twice.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
