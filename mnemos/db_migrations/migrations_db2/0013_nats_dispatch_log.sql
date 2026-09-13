--#SET TERMINATOR @
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
--
-- Same retcon-idempotency gap as the Oracle 0013 file (see its comment
-- for the full incident): the original version of this file was a bare
-- CREATE TABLE + CREATE INDEX with no per-file terminator, so it never
-- matched the guarded EXECUTE IMMEDIATE / CONTINUE HANDLER idiom the rest
-- of this backend uses (see 0005_oauth_identities.sql) -- a database
-- still on the legacy pre-retcon shape would hit "table already exists"
-- on CREATE TABLE (swallowed as benign replay) and then a real failure
-- on CREATE INDEX ... (dispatched_at DESC), which has no such column on
-- the legacy table. Legacy columns are dead (see above), so a
-- legacy-shaped table is safe to drop and recreate -- only in-flight
-- dedupe markers are lost.

-- Drop the table ONLY if it exists and is still in the legacy shape.
-- Dynamic (EXECUTE IMMEDIATE) so this carries no static reference to
-- nats_dispatch_log -- a static DROP would fail to compile on a fresh
-- database where the table does not exist yet.
BEGIN
  FOR c AS
    SELECT COUNT(*) AS has_canonical FROM SYSCAT.COLUMNS
     WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'NATS_DISPATCH_LOG' AND COLNAME = 'DISPATCHED_AT'
  DO
    IF c.has_canonical = 0
       AND EXISTS (SELECT 1 FROM SYSCAT.TABLES
                    WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'NATS_DISPATCH_LOG')
    THEN
      EXECUTE IMMEDIATE 'DROP TABLE nats_dispatch_log';
    END IF;
  END FOR;
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE nats_dispatch_log (
      event_id      VARCHAR(128)                        NOT NULL,
      subject       VARCHAR(256)                        NOT NULL,
      dispatched_at TIMESTAMP(6) DEFAULT CURRENT TIMESTAMP NOT NULL,
      PRIMARY KEY (event_id, subject)
    )';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_nats_dispatch_log_dispatched_at ON nats_dispatch_log (dispatched_at DESC)';
END@
