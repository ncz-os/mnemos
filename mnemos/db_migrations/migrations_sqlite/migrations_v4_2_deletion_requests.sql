-- Durable GDPR request queue for the SQLite edge profile.
CREATE TABLE IF NOT EXISTS deletion_requests (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    target_user_id TEXT NOT NULL,
    target_namespace TEXT,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (
        status IN ('requested','confirmed','sweep_verifying','soft_deleted',
                   'hard_deleting','hard_deleted','restored','cancelled')
    ),
    notes TEXT,
    soft_deleted_at TEXT,
    restore_by TEXT,
    hard_deleted_at TEXT,
    restored_at TEXT
);

CREATE INDEX IF NOT EXISTS deletion_requests_status_idx
    ON deletion_requests(status, confirmed_at, requested_at);

CREATE UNIQUE INDEX IF NOT EXISTS deletion_requests_active_unique_idx
    ON deletion_requests(target_user_id, COALESCE(target_namespace, ''))
    WHERE status IN ('requested','confirmed','sweep_verifying','soft_deleted','hard_deleting');
