-- 0013_nats_dispatch_log.sql — Oracle 26ai port for MNEMOS parity (item 9).
--
-- Item 9 retconned the legacy Oracle shape `(id, subject, payload,
-- published_at, acked_at)` to the canonical Postgres/SQLite
-- `(event_id, subject, dispatched_at)` shape with a real unique
-- constraint on (event_id, subject). The canonical shape is what every
-- NATS dispatch-log dedupe call site
-- (mnemos/workers/webhooks_dispatch_nats_consumer.py::_record_dispatch_once
-- and
-- mnemos/workers/federation_memory_nats_consumer.py::_store_memory_upsert)
-- actually needs: an idempotent "have I dispatched this (event_id,
-- subject) pair before" check.
--
-- The legacy `payload`/`published_at`/`acked_at` columns were never
-- read by any production code (grep `nats_dispatch_log` across the repo
-- confirms only the two NATS consumer files and these migrations
-- reference the table) — they appear to have been an early-iteration
-- outbox shape that never reconciled with the Postgres/SQLite canonical
-- shape. Item 9 retcon makes them match.
--
-- A real PRIMARY KEY (event_id, subject) is the dedupe primitive the
-- `NatsDispatchLogRepository.record_if_new` ABC relies on; concurrent
-- redeliveries cannot both insert. The Oracle MERGE / INSERT … ON
-- CONFLICT combination (record_if_new impl in mnemos/persistence/oracle.py)
-- translates the conflict into a clean `False` return.
--
-- The retcon above rewrote this file's CREATE TABLE in place instead of
-- adding a new migration, so it is only idempotent for a fresh install or
-- a database that already has the canonical shape (both cases fall
-- through to ORA-00955 "name already used", which the Python migration
-- runner's _is_benign_oracle_error already swallows). A database that
-- provisioned this table under the ORIGINAL pre-retcon migration still
-- has the legacy `(id, subject, payload, published_at, acked_at)` shape,
-- so ORA-00955 hides that CREATE TABLE was a no-op and the very next
-- statement, `CREATE INDEX ... (dispatched_at DESC)`, fails for real with
-- ORA-00904 "DISPATCHED_AT": invalid identifier -- there is no such
-- column on the legacy table. Found live on a database that had run this
-- table's original migration months before the retcon (2026-09-13);
-- crash-looped `mnemos serve` on every restart until this fix.
--
-- The legacy columns are dead (see above: never read by any production
-- code), so a legacy-shaped table is safe to drop and recreate -- the
-- only loss is in-flight dedupe markers, which just means a NATS event
-- already handled once might be redelivered and reprocessed once, which
-- every consumer already tolerates as an at-least-once bus.
DECLARE
    v_has_canonical_shape NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_has_canonical_shape
      FROM user_tab_columns
     WHERE table_name = 'NATS_DISPATCH_LOG'
       AND column_name = 'DISPATCHED_AT';
    IF v_has_canonical_shape = 0 THEN
        BEGIN
            EXECUTE IMMEDIATE 'DROP TABLE nats_dispatch_log';
        EXCEPTION
            WHEN OTHERS THEN
                IF SQLCODE != -942 THEN -- table or view does not exist (fresh install: nothing to drop)
                    RAISE;
                END IF;
        END;
    END IF;
END;
/

CREATE TABLE nats_dispatch_log (
    event_id      VARCHAR2(128)                      NOT NULL,
    subject       VARCHAR2(256)                      NOT NULL,
    dispatched_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    PRIMARY KEY (event_id, subject)
);

CREATE INDEX idx_nats_dispatch_log_dispatched_at
    ON nats_dispatch_log (dispatched_at DESC);