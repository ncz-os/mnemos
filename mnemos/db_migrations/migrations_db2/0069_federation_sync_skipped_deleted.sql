-- migration: 0069_federation_sync_skipped_deleted
-- purpose:   F-2 — count the inbound events whose replica row was
--            SOFT-DELETED locally and therefore could not be applied.
--            ``fetch_federated_memory_marker`` used to hide a soft-deleted
--            replica, so ``_store_memories`` read it as "absent", the
--            insert-or-ignore wrote nothing, and the row stayed deleted
--            with no number anywhere. The marker lookup now returns the
--            deleted row (with its lifecycle state) and the receiver
--            counts and logs the skip instead of discarding it in silence.
-- target:    IBM Db2 12.1 (Oracle Compat mode)
-- note:      replay raises SQLSTATE 42711 (duplicate column), which
--            mnemos/persistence/schema.py treats as benign.

ALTER TABLE federation_sync_log ADD COLUMN skipped_deleted INTEGER DEFAULT 0 NOT NULL;
