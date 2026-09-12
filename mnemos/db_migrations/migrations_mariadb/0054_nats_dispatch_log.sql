-- ---------------------------------------------------------------------------
-- MNEMOS v6.4 NATS dispatch-log dedupe (item 9) — MariaDB arm.
--
-- MariaDB inherits the MySQL migration shape; the file is split because
-- the migration runner applies backend-specific directories separately.
-- See ``migrations_mysql/0054_nats_dispatch_log.sql`` for the full
-- dialect notes — the only delta here is that MariaDB 11.7+ DOES
-- support ``CREATE INDEX IF NOT EXISTS``, so the dispatched_at index
-- can be declared standalone if preferred; keeping it inline mirrors
-- the MySQL arm for symmetry and avoids the IF NOT EXISTS portability
-- question.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS nats_dispatch_log (
    event_id      VARCHAR(128)   NOT NULL,
    subject       VARCHAR(256)   NOT NULL,
    dispatched_at DATETIME(6)    NOT NULL DEFAULT NOW(6),

    PRIMARY KEY (event_id, subject)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
;

KEY idx_nats_dispatch_log_dispatched_at (dispatched_at);