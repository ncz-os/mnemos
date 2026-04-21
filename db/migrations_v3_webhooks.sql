-- ---------------------------------------------------------------------------
-- MNEMOS v3.0.0 migration: webhook subscriptions + delivery log
--
-- Adds outbound notifications on memory and consultation events.
-- Deliveries are logged per attempt for audit and retry replay on restart.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    url             TEXT         NOT NULL,
    events          TEXT[]       NOT NULL,                 -- e.g. ['memory.created', 'consultation.completed']
    secret          TEXT         NOT NULL,                 -- HMAC-SHA256 signing secret (never returned after create)
    description     TEXT,
    owner_id        TEXT         NOT NULL DEFAULT 'default' REFERENCES users(id) ON DELETE CASCADE,
    namespace       TEXT         NOT NULL DEFAULT 'default',
    created         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    revoked         BOOLEAN      NOT NULL DEFAULT FALSE,
    revoked_at      TIMESTAMPTZ,

    CONSTRAINT webhook_url_format CHECK (url LIKE 'http://%' OR url LIKE 'https://%'),
    CONSTRAINT webhook_events_nonempty CHECK (array_length(events, 1) > 0)
);

CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_owner
    ON webhook_subscriptions(owner_id) WHERE NOT revoked;

CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_events
    ON webhook_subscriptions USING gin(events) WHERE NOT revoked;

-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id  UUID         NOT NULL REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,
    event_type       TEXT         NOT NULL,
    payload          TEXT         NOT NULL,                 -- the JSON body we sent (or plan to send)
    payload_hash     TEXT         NOT NULL,                 -- SHA-256 hex of payload bytes
    attempt_num      INTEGER      NOT NULL DEFAULT 1,

    -- status: pending | succeeded | failed | retrying | abandoned
    status           TEXT         NOT NULL DEFAULT 'pending',
    response_status  INTEGER,
    response_body    TEXT,
    error            TEXT,
    scheduled_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),   -- when this attempt should fire
    delivered_at     TIMESTAMPTZ,
    created          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_subscription
    ON webhook_deliveries(subscription_id, created DESC);

-- Worker picks up pending/retrying rows past their scheduled_at
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_pending
    ON webhook_deliveries(scheduled_at)
    WHERE status IN ('pending', 'retrying');
