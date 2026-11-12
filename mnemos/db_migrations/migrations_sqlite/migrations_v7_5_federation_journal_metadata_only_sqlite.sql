-- 0072: do not journal writes that cannot change federated content (SQLite).
-- Mirror of migrations/0072_federation_journal_metadata_only.sql; the
-- rationale, the cause breakdown and the measured counts live there and in
-- docs/FEDERATION_JOURNAL.md.
--
-- SQLite triggers cannot run procedural bodies, so the CONTENT / METADATA-ONLY
-- split is expressed as two disjoint WHEN windows over the same columns the
-- Postgres function branches on:
--
--   * ``fed_journal_update`` -- at least one CONTENT column changed; appends
--     one event (this is the 0068 predicate plus metadata/verbatim_content).
--     The metadata-only columns are deliberately NOT in this window, so a
--     recall bump can never match it.
--   * ``fed_journal_update_metadata`` -- recall bookkeeping changed. It appends
--     nothing; it removes any pending metadata-only rows for that memory.
--
-- Because the metadata-only columns are absent from fed_journal_update's
-- predicate, the two windows are disjoint by construction: no UPDATE can fire
-- both, and their relative order therefore does not matter.

ALTER TABLE federation_changes ADD COLUMN event_kind TEXT DEFAULT 'content';

CREATE INDEX IF NOT EXISTS idx_fed_changes_memory_event ON federation_changes(memory_id, event_id);

DROP TRIGGER IF EXISTS fed_journal_update;
DROP TRIGGER IF EXISTS fed_journal_update_content;
DROP TRIGGER IF EXISTS fed_journal_update_metadata;

CREATE TRIGGER fed_journal_update_content AFTER UPDATE OF federation_source,content,category,namespace,owner_id,permission_mode,deleted_at,archived_at,consolidated_into,embedding,metadata,verbatim_content ON memories
WHEN ((OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault'))
   OR (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault')))
 AND (OLD.federation_source IS NOT NEW.federation_source
   OR OLD.content IS NOT NEW.content
   OR OLD.category IS NOT NEW.category
   OR OLD.namespace IS NOT NEW.namespace
   OR OLD.owner_id IS NOT NEW.owner_id
   OR OLD.permission_mode IS NOT NEW.permission_mode
   OR OLD.deleted_at IS NOT NEW.deleted_at
   OR OLD.archived_at IS NOT NEW.archived_at
   OR OLD.consolidated_into IS NOT NEW.consolidated_into
   OR OLD.embedding IS NOT NEW.embedding
   OR OLD.metadata IS NOT NEW.metadata
   OR OLD.verbatim_content IS NOT NEW.verbatim_content)
BEGIN
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) SELECT NEW.id,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),OLD.namespace,OLD.category,CASE WHEN (OLD.permission_mode % 10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NEW.namespace,NEW.category,CASE WHEN (NEW.permission_mode % 10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END,'content';
END;

CREATE TRIGGER fed_journal_update_metadata AFTER UPDATE OF recall_count,last_recalled_at ON memories
WHEN ((OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault'))
   OR (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault')))
 AND (OLD.recall_count IS NOT NEW.recall_count
   OR OLD.last_recalled_at IS NOT NEW.last_recalled_at)
 AND NOT (OLD.federation_source IS NOT NEW.federation_source
   OR OLD.content IS NOT NEW.content
   OR OLD.category IS NOT NEW.category
   OR OLD.namespace IS NOT NEW.namespace
   OR OLD.owner_id IS NOT NEW.owner_id
   OR OLD.permission_mode IS NOT NEW.permission_mode
   OR OLD.deleted_at IS NOT NEW.deleted_at
   OR OLD.archived_at IS NOT NEW.archived_at
   OR OLD.consolidated_into IS NOT NEW.consolidated_into
   OR OLD.embedding IS NOT NEW.embedding
   OR OLD.metadata IS NOT NEW.metadata
   OR OLD.verbatim_content IS NOT NEW.verbatim_content)
BEGIN
 DELETE FROM federation_changes
  WHERE memory_id = NEW.id AND seq IS NULL AND event_kind = 'metadata';
END;

-- Inserts and deletes always carry content.
DROP TRIGGER IF EXISTS fed_journal_insert;
CREATE TRIGGER fed_journal_insert AFTER INSERT ON memories WHEN (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault'))
BEGIN
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) SELECT NEW.id,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),NULL,NULL,0,0,NEW.namespace,NEW.category,CASE WHEN (NEW.permission_mode % 10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END,'content';
END;
DROP TRIGGER IF EXISTS fed_journal_delete;
CREATE TRIGGER fed_journal_delete AFTER DELETE ON memories WHEN (OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault'))
BEGIN
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) SELECT OLD.id,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),OLD.namespace,OLD.category,CASE WHEN (OLD.permission_mode % 10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NULL,NULL,0,0,'content';
END;
