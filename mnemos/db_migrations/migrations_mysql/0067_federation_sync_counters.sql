-- migration: 0067_federation_sync_counters
-- purpose:   F-1/F-5 — separate the WIRE event count from the APPLIED row
--            count in the federation sync log, and record how many applied
--            rows still carry no embedding.
--            ``memories_pulled`` counted ``len(batch)`` (wire events) before
--            the durable sequence fence ran, so every journal replay
--            inflated ``federation_peers.total_pulled`` by the number of
--            duplicate events (the 4.4 M-against-25 k symptom on the
--            PROTEUS replica). ``events_received`` keeps the wire count
--            visible; ``rows_without_embedding`` exposes the F-5 embedding
--            gap per sync.
-- target:    MySQL 8 / MariaDB 11
-- note:      MySQL has no ``ADD COLUMN IF NOT EXISTS``, so replaying
--            raises 1060 (duplicate column); mnemos/persistence/mysql.py
--            adds these columns through ``_ensure_mysql_columns``, which
--            checks information_schema first. This file keeps the change
--            in the numbered migration manifest for parity.

ALTER TABLE federation_sync_log
  ADD COLUMN events_received INT NOT NULL DEFAULT 0;

ALTER TABLE federation_sync_log
  ADD COLUMN rows_without_embedding INT NOT NULL DEFAULT 0;