-- migration: 0066_webhook_repository_parity
-- PostgreSQL already carries the full row-per-attempt webhook schema.  Keep
-- the description addition explicit and replay-safe for cross-backend parity.

ALTER TABLE webhook_subscriptions
    ADD COLUMN IF NOT EXISTS description TEXT;
