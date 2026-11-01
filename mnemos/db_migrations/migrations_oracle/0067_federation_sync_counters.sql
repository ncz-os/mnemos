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
-- target:    Oracle 23ai
-- note:      replay raises ORA-01430 (column already exists), which
--            mnemos/persistence/schema.py treats as benign.

ALTER TABLE federation_sync_log ADD (
    events_received NUMBER DEFAULT 0 NOT NULL,
    rows_without_embedding NUMBER DEFAULT 0 NOT NULL
);