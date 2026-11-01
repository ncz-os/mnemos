-- migration: 0069_federation_sync_skipped_deleted
-- purpose:   F-2 — count the inbound events whose replica row was
--            SOFT-DELETED locally and therefore could not be applied.
--            ``fetch_federated_memory_marker`` used to hide a soft-deleted
--            replica, so ``_store_memories`` read it as "absent", the
--            insert-or-ignore wrote nothing, and the row stayed deleted
--            with no number anywhere. The marker lookup now returns the
--            deleted row (with its lifecycle state) and the receiver
--            counts and logs the skip instead of discarding it in silence.
-- target:    SQLite
-- note:      additive and idempotent — ``ALTER TABLE ADD COLUMN`` cannot
--            be wrapped in IF NOT EXISTS, so a replay against an already
--            migrated database raises "duplicate column name", which
--            mnemos/persistence/sqlite.py treats as a no-op. The column
--            also arrives through ``_ensure_columns`` on every open.

ALTER TABLE federation_sync_log ADD COLUMN skipped_deleted INTEGER NOT NULL DEFAULT 0;

INSERT OR REPLACE INTO schema_marker(name,applied) VALUES('0069_federation_sync_skipped_deleted.sql',1);
