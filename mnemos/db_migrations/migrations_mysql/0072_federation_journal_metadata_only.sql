-- 0072: do not journal writes that cannot change federated content (MySQL).
-- Mirror of migrations/0072_federation_journal_metadata_only.sql; the
-- rationale, the cause breakdown and the measured counts live there and in
-- docs/FEDERATION_JOURNAL.md.
--
-- The trigger is column-scoped exactly as 0068's is, narrowed to the columns
-- that actually decide the branch (dropping ``updated`` and the ``source_*``
-- bookkeeping columns, which 0068 already removed from the predicate -- a
-- column in UPDATE OF with no predicate usage only ever added trigger
-- executions, never events). That is what lets the METADATA-ONLY branch absorb
-- rather than merely stay silent: a statement that only bumps recall
-- bookkeeping names no content column, yet must still drop any pending
-- metadata-only rows for the memory. A trigger may DELETE from a different
-- table it has not SELECTed from, so the branch is a single DELETE, never a
-- SELECT-then-WRITE pair.

ALTER TABLE federation_changes ADD COLUMN event_kind VARCHAR(16) DEFAULT 'content';

CREATE INDEX idx_fed_changes_memory_event ON federation_changes(memory_id, event_id);

DROP TRIGGER IF EXISTS fed_journal_update;
CREATE TRIGGER fed_journal_update AFTER UPDATE OF federation_source,content,category,namespace,owner_id,permission_mode,deleted_at,archived_at,consolidated_into,embedding,metadata,verbatim_content,recall_count,last_recalled_at ON memories FOR EACH ROW
BEGIN
 IF ((OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault')) OR (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault'))) AND (NOT (OLD.federation_source <=> NEW.federation_source) OR NOT (OLD.content <=> NEW.content) OR NOT (OLD.category <=> NEW.category) OR NOT (OLD.namespace <=> NEW.namespace) OR NOT (OLD.owner_id <=> NEW.owner_id) OR NOT (OLD.permission_mode <=> NEW.permission_mode) OR NOT (OLD.deleted_at <=> NEW.deleted_at) OR NOT (OLD.archived_at <=> NEW.archived_at) OR NOT (OLD.consolidated_into <=> NEW.consolidated_into) OR NOT (OLD.embedding <=> NEW.embedding) OR NOT (OLD.metadata <=> NEW.metadata) OR NOT (OLD.verbatim_content <=> NEW.verbatim_content) OR NOT (OLD.recall_count <=> NEW.recall_count) OR NOT (OLD.last_recalled_at <=> NEW.last_recalled_at)) THEN
  IF (NOT (OLD.federation_source <=> NEW.federation_source) OR NOT (OLD.content <=> NEW.content) OR NOT (OLD.category <=> NEW.category) OR NOT (OLD.namespace <=> NEW.namespace) OR NOT (OLD.owner_id <=> NEW.owner_id) OR NOT (OLD.permission_mode <=> NEW.permission_mode) OR NOT (OLD.deleted_at <=> NEW.deleted_at) OR NOT (OLD.archived_at <=> NEW.archived_at) OR NOT (OLD.consolidated_into <=> NEW.consolidated_into) OR NOT (OLD.embedding <=> NEW.embedding) OR NOT (OLD.metadata <=> NEW.metadata) OR NOT (OLD.verbatim_content <=> NEW.verbatim_content)) THEN
   INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) VALUES (NEW.id,UTC_TIMESTAMP(6),OLD.namespace,OLD.category,CASE WHEN MOD(OLD.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NEW.namespace,NEW.category,CASE WHEN MOD(NEW.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END,'content');
  ELSE
   DELETE FROM federation_changes WHERE memory_id = NEW.id AND seq IS NULL AND event_kind = 'metadata';
  END IF;
 END IF;
END;

DROP TRIGGER IF EXISTS fed_journal_insert;
CREATE TRIGGER fed_journal_insert AFTER INSERT ON memories FOR EACH ROW
BEGIN
 IF (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault')) THEN
  INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) VALUES (NEW.id,UTC_TIMESTAMP(6),NULL,NULL,0,0,NEW.namespace,NEW.category,CASE WHEN MOD(NEW.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END,'content');
 END IF;
END;

DROP TRIGGER IF EXISTS fed_journal_delete;
CREATE TRIGGER fed_journal_delete AFTER DELETE ON memories FOR EACH ROW
BEGIN
 IF (OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault')) THEN
  INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) VALUES (OLD.id,UTC_TIMESTAMP(6),OLD.namespace,OLD.category,CASE WHEN MOD(OLD.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NULL,NULL,0,0,'content');
 END IF;
END;
