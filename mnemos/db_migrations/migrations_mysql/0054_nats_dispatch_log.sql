-- ---------------------------------------------------------------------------
-- MNEMOS v6.4 NATS dispatch-log dedupe (item 9)
--
-- Creates the canonical ``nats_dispatch_log`` table for MySQL / MariaDB so
-- the backend-neutral ``NatsDispatchLogRepository.record_if_new`` ABC can
-- serve both the webhook outbox NATS consumer
-- (``mnemos/workers/webhooks_dispatch_nats_consumer.py``) and the
-- federation memory upsert NATS consumer
-- (``mnemos/workers/federation_memory_nats_consumer.py``) on the MySQL /
-- MariaDB family. Postgres + SQLite have shipped this table since v5.2.0;
-- Oracle + Db2 got the canonical shape (event_id, subject, dispatched_at)
-- in this same item; MySQL + MariaDB had no migration at all before this
-- item — the two NATS consumers fell off a cliff on MySQL/MariaDB because
-- the table did not exist.
--
-- Dialect adaptations vs Postgres / SQLite:
--
-- * Postgres/SQLite ``TIMESTAMPTZ`` -> ``DATETIME(6)``. UTC is pinned at
--   session level by ``SET time_zone='+00:00'`` in MysqlBackend.open and
--   MariadbBackend.open, so DATETIME(6) values are wall-clock UTC.
-- * MySQL/MariaDB primary-key semantics are the dedupe primitive:
--   ``INSERT IGNORE`` (record_if_new impl in
--   ``mnemos/persistence/mysql.py``) translates the existing-row case
--   into ``rowcount == 0`` without raising. ``INSERT ... ON DUPLICATE
--   KEY UPDATE id = id`` would also work but emits a ``rows_affected``
--   of 2 on update, which complicates the "is this a fresh insert"
--   check; ``INSERT IGNORE`` returns 0 cleanly.
-- * The Postgres / SQLite ``PRIMARY KEY (event_id, subject)`` is the
--   unique-constraint primitive that makes the dedupe race-safe:
--   concurrent redeliveries on the same (event_id, subject) pair
--   cannot both insert.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS nats_dispatch_log (
    event_id      VARCHAR(128)   NOT NULL,
    subject       VARCHAR(256)   NOT NULL,
    dispatched_at DATETIME(6)    NOT NULL DEFAULT NOW(6),

    PRIMARY KEY (event_id, subject)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
;

-- CREATE TABLE IF NOT EXISTS already makes the inline index idempotent;
-- real MySQL 8 (as opposed to MariaDB) does not support IF NOT EXISTS
-- on CREATE INDEX, so the index must be inside CREATE TABLE for the
-- MySQL arm. See 0040_webhook_repository.sql for the same rationale.
KEY idx_nats_dispatch_log_dispatched_at (dispatched_at);