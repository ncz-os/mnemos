-- Lightweight, multi-valued memory tags for project scoping.
-- Tags are retrieval metadata, not a visibility or versioning boundary.
BEGIN;

CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id TEXT        NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag       TEXT        NOT NULL,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (memory_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag);

DO $$ BEGIN
    GRANT SELECT, INSERT, DELETE ON memory_tags TO mnemos_user;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

COMMIT;
