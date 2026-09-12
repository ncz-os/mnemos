-- 0013_nats_dispatch_log.sql — Oracle 23ai port for MNEMOS parity (item 9).
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

CREATE TABLE nats_dispatch_log (
    event_id      VARCHAR2(128)                      NOT NULL,
    subject       VARCHAR2(256)                      NOT NULL,
    dispatched_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    PRIMARY KEY (event_id, subject)
);

CREATE INDEX idx_nats_dispatch_log_dispatched_at
    ON nats_dispatch_log (dispatched_at DESC);