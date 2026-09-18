-- migration: 0066_webhook_repository_parity
-- purpose:   Bring the Oracle webhook tables created by 0001_core_schema.sql
--            up to the schema consumed by OracleWebhookRepository.
--
-- The subscription table already uses created_at; add the optional description
-- field shared by the other backends.  The delivery table predates the
-- row-per-attempt repository contract, so retain its legacy columns for old
-- writers and add/backfill the lease-aware columns used by current code.
--
-- Oracle schema provisioning replays every migration at startup.  ADD/MODIFY
-- replays are safe because schema.py treats ORA-01430/ORA-01442/ORA-01451 as
-- benign for ALTER statements; index and trigger recreation is likewise
-- idempotent under the existing Oracle migration runner.

ALTER TABLE webhook_subscriptions ADD (description CLOB);

ALTER TABLE webhook_deliveries ADD (payload_hash VARCHAR2(64));
ALTER TABLE webhook_deliveries ADD (attempt_num NUMBER(10));
ALTER TABLE webhook_deliveries ADD (status VARCHAR2(32));
ALTER TABLE webhook_deliveries ADD (response_status NUMBER(10));
ALTER TABLE webhook_deliveries ADD (response_body CLOB);
ALTER TABLE webhook_deliveries ADD (error CLOB);
ALTER TABLE webhook_deliveries ADD (scheduled_at TIMESTAMP WITH TIME ZONE);
ALTER TABLE webhook_deliveries ADD (delivered_at TIMESTAMP WITH TIME ZONE);
ALTER TABLE webhook_deliveries ADD (lease_token VARCHAR2(128));
ALTER TABLE webhook_deliveries ADD (lease_expires_at TIMESTAMP WITH TIME ZONE);
ALTER TABLE webhook_deliveries ADD (writer_revision NUMBER(10));
ALTER TABLE webhook_deliveries ADD (status_updated_at TIMESTAMP WITH TIME ZONE);
ALTER TABLE webhook_deliveries ADD (superseded NUMBER(1));

-- Backfill only missing new-shape values.  This predicate makes replay a no-op
-- for rows already written by the current repository and preserves their live
-- status/lease state.  Legacy writer_revision remains 0 by design so current
-- recovery workers never mistake an old row for a lease-aware write.
UPDATE webhook_deliveries
SET payload_hash = COALESCE(
        payload_hash,
        LOWER(RAWTOHEX(STANDARD_HASH(
            COALESCE(DBMS_LOB.SUBSTR(payload, 32767, 1), id),
            'SHA256'
        )))
    ),
    attempt_num = COALESCE(attempt_num, GREATEST(NVL(attempt_count, 0) + 1, 1)),
    status = COALESCE(
        status,
        CASE LOWER(NVL(state, 'pending'))
            WHEN 'delivered' THEN 'succeeded'
            WHEN 'succeeded' THEN 'succeeded'
            WHEN 'failed' THEN 'abandoned'
            WHEN 'abandoned' THEN 'abandoned'
            WHEN 'retrying' THEN 'retrying'
            ELSE 'pending'
        END
    ),
    error = COALESCE(error, last_error),
    scheduled_at = COALESCE(scheduled_at, next_attempt_at, created_at, SYSTIMESTAMP),
    writer_revision = COALESCE(writer_revision, 0),
    status_updated_at = COALESCE(status_updated_at, updated_at, created_at, SYSTIMESTAMP),
    superseded = COALESCE(superseded, 0)
WHERE payload_hash IS NULL
   OR attempt_num IS NULL
   OR status IS NULL
   OR scheduled_at IS NULL
   OR writer_revision IS NULL
   OR status_updated_at IS NULL
   OR superseded IS NULL;

ALTER TABLE webhook_deliveries MODIFY (payload_hash NOT NULL);
ALTER TABLE webhook_deliveries MODIFY (attempt_num DEFAULT 1 NOT NULL);
ALTER TABLE webhook_deliveries MODIFY (status DEFAULT 'pending' NOT NULL);
ALTER TABLE webhook_deliveries MODIFY (scheduled_at DEFAULT SYSTIMESTAMP NOT NULL);
ALTER TABLE webhook_deliveries MODIFY (writer_revision DEFAULT 0 NOT NULL);
ALTER TABLE webhook_deliveries MODIFY (status_updated_at DEFAULT SYSTIMESTAMP NOT NULL);
ALTER TABLE webhook_deliveries MODIFY (superseded DEFAULT 0 NOT NULL);

-- Repair any historical duplicate live/succeeded rows before installing the
-- row-per-attempt uniqueness guarantees.
MERGE INTO webhook_deliveries d
USING (
    SELECT id
    FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY subscription_id, event_type, payload_hash, attempt_num
                   ORDER BY created_at DESC, id DESC
               ) AS duplicate_rank
        FROM webhook_deliveries
        WHERE status IN ('pending', 'retrying') AND superseded = 0
    )
    WHERE duplicate_rank > 1
) duplicate_live
ON (d.id = duplicate_live.id)
WHEN MATCHED THEN UPDATE SET
    d.status = 'abandoned',
    d.superseded = 1,
    d.lease_token = NULL,
    d.lease_expires_at = NULL,
    d.error = COALESCE(d.error, TO_CLOB('superseded duplicate live retry attempt'));

MERGE INTO webhook_deliveries d
USING (
    SELECT id
    FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY subscription_id, event_type, payload_hash
                   ORDER BY attempt_num ASC, created_at ASC, id ASC
               ) AS duplicate_rank
        FROM webhook_deliveries
        WHERE status = 'succeeded'
    )
    WHERE duplicate_rank > 1
) duplicate_success
ON (d.id = duplicate_success.id)
WHEN MATCHED THEN UPDATE SET
    d.status = 'abandoned',
    d.superseded = 1,
    d.lease_token = NULL,
    d.lease_expires_at = NULL;

CREATE INDEX idx_webhook_deliveries_pending
    ON webhook_deliveries(scheduled_at);
CREATE INDEX idx_webhook_deliveries_lease_expires_at
    ON webhook_deliveries(lease_expires_at);

CREATE UNIQUE INDEX uq_webhook_deliveries_live_chain_attempt
    ON webhook_deliveries(
        CASE WHEN status IN ('pending', 'retrying') AND superseded = 0 THEN subscription_id END,
        CASE WHEN status IN ('pending', 'retrying') AND superseded = 0 THEN event_type END,
        CASE WHEN status IN ('pending', 'retrying') AND superseded = 0 THEN payload_hash END,
        CASE WHEN status IN ('pending', 'retrying') AND superseded = 0 THEN attempt_num END
    );

CREATE UNIQUE INDEX uq_webhook_deliveries_succeeded_chain
    ON webhook_deliveries(
        CASE WHEN status = 'succeeded' THEN subscription_id END,
        CASE WHEN status = 'succeeded' THEN event_type END,
        CASE WHEN status = 'succeeded' THEN payload_hash END
    );

CREATE OR REPLACE TRIGGER trg_webhook_deliveries_status_updated_at
BEFORE UPDATE ON webhook_deliveries
FOR EACH ROW
BEGIN
    IF :OLD.status IS NULL OR :OLD.status <> :NEW.status THEN
        :NEW.status_updated_at := SYSTIMESTAMP;
    END IF;
END;
/

CREATE OR REPLACE TRIGGER webhook_deliveries_succeeded_terminal
BEFORE UPDATE ON webhook_deliveries
FOR EACH ROW
BEGIN
    IF :OLD.status = 'succeeded' AND :NEW.status <> 'succeeded' THEN
        RAISE_APPLICATION_ERROR(
            -20001,
            'webhook_deliveries: cannot transition status away from succeeded'
        );
    END IF;
END;
/
