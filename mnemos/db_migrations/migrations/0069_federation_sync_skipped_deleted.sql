-- migration: 0069_federation_sync_skipped_deleted
-- purpose:   F-2 — count the inbound events whose replica row was
--            SOFT-DELETED locally and therefore could not be applied.
--            ``fetch_federated_memory_marker`` used to hide a soft-deleted
--            replica, so ``_store_memories`` read it as "absent", the
--            insert-or-ignore wrote nothing, and the row stayed deleted
--            with no number anywhere -- a silent divergence between the
--            replica and its source. The marker lookup now returns the
--            deleted row (with its lifecycle state) and the receiver
--            counts and logs the skip instead of discarding it in silence.
--            Undelete is a policy decision and stays out of the receiver.
-- target:    PostgreSQL 16
-- note:      additive; ``ADD COLUMN IF NOT EXISTS`` makes the replay a
--            no-op.

ALTER TABLE federation_sync_log
  ADD COLUMN IF NOT EXISTS skipped_deleted INTEGER NOT NULL DEFAULT 0;
