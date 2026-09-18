-- migration: 0066_webhook_repository_parity
-- Db2 shares OracleWebhookRepository's subscription CRUD SQL, so it needs the
-- same optional description column.  SQLSTATE 42711 is an idempotent replay in
-- mnemos.persistence.schema.

ALTER TABLE webhook_subscriptions ADD COLUMN description CLOB;
