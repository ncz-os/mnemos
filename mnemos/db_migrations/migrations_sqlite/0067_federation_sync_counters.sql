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
-- target:    SQLite
-- note:      additive and idempotent — ``ALTER TABLE ADD COLUMN`` cannot
--            be wrapped in IF NOT EXISTS, so a replay against an already
--            migrated database raises "duplicate column name", which
--            mnemos/persistence/sqlite.py treats as a no-op.

ALTER TABLE federation_sync_log ADD COLUMN events_received INTEGER NOT NULL DEFAULT 0;
ALTER TABLE federation_sync_log ADD COLUMN rows_without_embedding INTEGER NOT NULL DEFAULT 0;

INSERT OR REPLACE INTO schema_marker(name,applied) VALUES('0067_federation_sync_counters.sql',1);
