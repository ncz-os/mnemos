-- 0013_nats_dispatch_log.sql — Db2 12.1.5 (Oracle Compat) port (item 9).
--
-- Item 9 retconned the legacy Db2 shape `(id, subject, payload,
-- published_at, acked_at)` to the canonical Postgres/SQLite
-- `(event_id, subject, dispatched_at)` shape with a real unique
-- constraint on (event_id, subject). See the Oracle 0013 file for the
-- full reconciliation rationale; the legacy Db2 columns were never
-- read by any production code.
--
-- Db2 native SQL tokens (CURRENT TIMESTAMP, TIMESTAMP(6) without TIME
-- ZONE — Db2 12.1.x stores a per-row clock value rather than a tz-aware
-- column type, see the comments in 0001_core_schema.sql for the same
-- pattern). The dispatched_at clock here is sufficient for the dedupe
-- log's purpose: a per-(event_id, subject) timestamp of when we first
-- observed the delivery, used only as a recency hint for the optional
-- idx_nats_dispatch_log_dispatched_at index.

CREATE TABLE nats_dispatch_log (
    event_id      VARCHAR(128)                        NOT NULL,
    subject       VARCHAR(256)                        NOT NULL,
    dispatched_at TIMESTAMP(6) DEFAULT CURRENT TIMESTAMP NOT NULL,
    PRIMARY KEY (event_id, subject)
);

CREATE INDEX idx_nats_dispatch_log_dispatched_at
    ON nats_dispatch_log (dispatched_at DESC);