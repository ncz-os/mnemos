-- Parity restatement. PostgreSQL already creates model_registry_sync_log in the
-- standalone migrations_model_registry.sql, which is applied outside the
-- numbered sequence. The parity gate requires the same numbered basename in
-- migrations/, migrations_oracle/ and migrations_db2/, so this file restates
-- the identical end state idempotently. On any database that ran
-- migrations_model_registry.sql this is a no-op.
CREATE TABLE IF NOT EXISTS model_registry_sync_log (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider          VARCHAR(50) NOT NULL,
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    models_found      INT         NOT NULL DEFAULT 0,
    models_added      INT         NOT NULL DEFAULT 0,
    models_updated    INT         NOT NULL DEFAULT 0,
    models_deprecated INT         NOT NULL DEFAULT 0,
    error             TEXT,
    duration_ms       INT
);

CREATE INDEX IF NOT EXISTS idx_model_registry_sync_log_provider  ON model_registry_sync_log(provider);
CREATE INDEX IF NOT EXISTS idx_model_registry_sync_log_synced_at ON model_registry_sync_log(synced_at DESC);
