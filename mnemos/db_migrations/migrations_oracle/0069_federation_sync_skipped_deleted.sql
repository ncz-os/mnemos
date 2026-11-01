-- migration: 0069_federation_sync_skipped_deleted
-- purpose:   F-2 — count the inbound events whose replica row was
--            SOFT-DELETED locally and therefore could not be applied.
--            ``fetch_federated_memory_marker`` used to hide a soft-deleted
--            replica, so ``_store_memories`` read it as "absent", the
--            insert-or-ignore wrote nothing, and the row stayed deleted
--            with no number anywhere. The marker lookup now returns the
--            deleted row (with its lifecycle state) and the receiver
--            counts and logs the skip instead of discarding it in silence.
-- target:    Oracle 23ai
-- note:      replay raises ORA-01430 (column already exists), which
--            mnemos/persistence/schema.py treats as benign.

ALTER TABLE federation_sync_log ADD (
    skipped_deleted NUMBER DEFAULT 0 NOT NULL
);
