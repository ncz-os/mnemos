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
-- target:    PostgreSQL 16
-- note:      additive; ``ADD COLUMN IF NOT EXISTS`` makes the replay a
--            no-op.

ALTER TABLE federation_sync_log
  ADD COLUMN IF NOT EXISTS events_received INTEGER NOT NULL DEFAULT 0;

ALTER TABLE federation_sync_log
  ADD COLUMN IF NOT EXISTS rows_without_embedding INTEGER NOT NULL DEFAULT 0;