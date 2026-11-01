-- migration: 0069_federation_sync_skipped_deleted
-- purpose:   F-2 — count the inbound events whose replica row was
--            SOFT-DELETED locally and therefore could not be applied.
--            ``fetch_federated_memory_marker`` used to hide a soft-deleted
--            replica, so ``_store_memories`` read it as "absent", the
--            insert-or-ignore wrote nothing, and the row stayed deleted
--            with no number anywhere. The marker lookup now returns the
--            deleted row (with its lifecycle state) and the receiver
--            counts and logs the skip instead of discarding it in silence.
-- target:    MySQL 8 / MariaDB 11
-- note:      MySQL has no ``ADD COLUMN IF NOT EXISTS``, so replaying
--            raises 1060 (duplicate column); mnemos/persistence/mysql.py
--            adds these columns through ``_ensure_mysql_columns``, which
--            checks information_schema first. This file keeps the change
--            in the numbered migration manifest for parity.

ALTER TABLE federation_sync_log
  ADD COLUMN skipped_deleted INT NOT NULL DEFAULT 0;
