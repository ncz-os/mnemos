--#SET TERMINATOR @
-- Lifecycle-worker schema parity for already-populated Db2 databases.
-- Duplicate-column replay is handled by the runtime runner's SQLSTATE 42711
-- allow-list. VARCHAR widening preserves existing archive rows.

ALTER TABLE memory_branches ADD COLUMN deleted_at TIMESTAMP(6)@
ALTER TABLE entities ADD COLUMN owner_id VARCHAR(256) NOT NULL DEFAULT 'default'@
ALTER TABLE entities ADD COLUMN namespace VARCHAR(256) NOT NULL DEFAULT 'default'@
ALTER TABLE entities ADD COLUMN deleted_at TIMESTAMP(6)@
ALTER TABLE session_memory_injections ADD COLUMN deleted_at TIMESTAMP(6)@

ALTER TABLE memory_archive ALTER COLUMN id SET DATA TYPE VARCHAR(100)@
ALTER TABLE memory_archive ALTER COLUMN original_memory_id SET DATA TYPE VARCHAR(100)@
