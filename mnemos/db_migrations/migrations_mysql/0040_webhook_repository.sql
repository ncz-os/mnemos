-- ---------------------------------------------------------------------------
-- MNEMOS v6.3 webhook repository (MySQL/MariaDB family) - item 5 of 12
--
-- Mirrors the Postgres v3 + v3.5 webhook persistence shape so the
-- backend-neutral ``WebhookRepository`` ABC methods (item 2 of 12) can be
-- served from MySQL/MariaDB with the same row-per-attempt semantics
-- (Postgres item 3, SQLite item 4). Webhooks are still NOT delivered by
-- the MySQL/MariaDB fleet in production - the ``MysqlBackend.webhooks``
-- accessor fails closed with ``BackendCapabilityMissing`` - but the
-- storage layer is now consistent with the contract so a future delivery
-- worker can be plugged in.
--
-- Dialect adaptations vs Postgres / SQLite:
--
-- * Postgres/SQLite ``TIMESTAMPTZ`` -> ``DATETIME(6)`` (UTC is pinned at
--   session level by ``SET time_zone='+00:00'`` in MysqlBackend.open).
-- * Postgres partial unique indexes ``WHERE status IN (...)`` are not
--   natively supported by MySQL/MariaDB. We emulate them with generated
--   columns whose value is NULL for terminal rows; MySQL/MariaDB unique
--   indexes permit multiple NULLs, so terminal rows are free to repeat
--   while live rows still enforce one-attempt-per-chain.
-- * ``SELECT ... FOR UPDATE SKIP LOCKED`` is supported by both MySQL
--   8.0+ and MariaDB 10.6+ on InnoDB - this migration makes the
--   claim/lease operations use that primitive directly.
-- * ``RETURNING`` clauses from UPDATE do not exist in MySQL/MariaDB;
--   the repository SQL selects the claimed rows with a follow-up
--   ``SELECT`` against the same connection after the ``UPDATE``.
-- * The ``status_updated_at`` BEFORE UPDATE trigger replaces the
--   Postgres plpgsql trigger with a single ``IF(OLD.status<>NEW.status)``
--   expression inside ``SET NEW.<col>``.
-- * The ``succeeded terminal`` BEFORE UPDATE trigger uses ``SIGNAL
--   SQLSTATE '45000'`` instead of ``RAISE EXCEPTION`` so a status
--   transition away from 'succeeded' is rejected at the engine
--   boundary, identical to the Postgres trigger behavior.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id              VARCHAR(64)   NOT NULL DEFAULT (UUID()),
    url             TEXT         NOT NULL,
    events          JSON         NOT NULL,
    secret          TEXT         NOT NULL,
    description     TEXT         NULL,
    owner_id        VARCHAR(256) NOT NULL DEFAULT 'default',
    namespace       VARCHAR(256) NOT NULL DEFAULT 'default',
    created         DATETIME(6)  NOT NULL DEFAULT NOW(6),
    revoked         TINYINT(1)   NOT NULL DEFAULT 0,
    revoked_at      DATETIME(6)  NULL,
    PRIMARY KEY (id),
    INDEX idx_webhook_subscriptions_owner (owner_id, namespace)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id                VARCHAR(64)   NOT NULL DEFAULT (UUID()),
    subscription_id   VARCHAR(64)   NOT NULL,
    event_type        VARCHAR(256)  NOT NULL,
    payload           LONGTEXT      NOT NULL,
    payload_hash      VARCHAR(64)   NOT NULL,
    attempt_num       INT           NOT NULL DEFAULT 1,
    status            VARCHAR(32)   NOT NULL DEFAULT 'pending',
    response_status   INT           NULL,
    response_body     LONGTEXT      NULL,
    error             TEXT          NULL,
    scheduled_at      DATETIME(6)   NOT NULL DEFAULT NOW(6),
    delivered_at      DATETIME(6)   NULL,
    created           DATETIME(6)   NOT NULL DEFAULT NOW(6),
    lease_token       VARCHAR(64)   NULL,
    lease_expires_at  DATETIME(6)   NULL,
    writer_revision   INT           NOT NULL DEFAULT 1,
    status_updated_at DATETIME(6)   NOT NULL DEFAULT NOW(6),
    superseded        TINYINT(1)    NOT NULL DEFAULT 0,
    -- Partial-unique emulators (NULL when the row is terminal so MySQL/MariaDB
    -- allow multiple terminal rows per chain). See header note above.
    live_chain_key    VARCHAR(768)  GENERATED ALWAYS AS (
        CASE
            WHEN status IN ('pending', 'retrying') AND superseded = 0
            THEN CONCAT(subscription_id, '|', event_type, '|', payload_hash, '|', attempt_num)
            ELSE NULL
        END
    ) STORED,
    succeeded_chain_key VARCHAR(768) GENERATED ALWAYS AS (
        CASE
            WHEN status = 'succeeded'
            THEN CONCAT(subscription_id, '|', event_type, '|', payload_hash)
            ELSE NULL
        END
    ) STORED,
    PRIMARY KEY (id),
    CONSTRAINT fk_webhook_deliveries_subscription
        FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id) ON DELETE CASCADE
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
;

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_subscription
    ON webhook_deliveries(subscription_id, created);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_pending
    ON webhook_deliveries(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_lease_expires_at
    ON webhook_deliveries(lease_expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_live_chain_attempt
    ON webhook_deliveries(live_chain_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_succeeded_chain
    ON webhook_deliveries(succeeded_chain_key);

-- status transition clock (replaces Postgres
-- webhook_deliveries_set_status_updated_at plpgsql trigger).
DROP TRIGGER IF EXISTS webhook_deliveries_set_status_updated_at;
CREATE TRIGGER webhook_deliveries_set_status_updated_at
BEFORE UPDATE ON webhook_deliveries
FOR EACH ROW
  SET NEW.status_updated_at = IF(OLD.status <> NEW.status, NOW(6), OLD.status_updated_at);

-- Enforce that status='succeeded' is terminal at the engine boundary
-- (matches Postgres webhook_deliveries_enforce_succeeded_terminal).
DROP TRIGGER IF EXISTS webhook_deliveries_enforce_succeeded_terminal;
CREATE TRIGGER webhook_deliveries_enforce_succeeded_terminal
BEFORE UPDATE ON webhook_deliveries
FOR EACH ROW
BEGIN
  IF OLD.status = 'succeeded' AND NEW.status <> 'succeeded' THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'webhook_deliveries: cannot transition status away from succeeded';
  END IF;
END;
