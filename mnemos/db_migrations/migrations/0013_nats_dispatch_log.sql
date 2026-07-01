-- 0013_nats_dispatch_log.sql — PostgreSQL numbered parity anchor.
-- NATS outbox/dispatch idempotency is shipped by db/migrations_v5_2_0_nats_outbox_idempotency.sql.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
