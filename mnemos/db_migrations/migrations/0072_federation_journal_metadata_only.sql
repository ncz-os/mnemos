-- 0072: do not journal writes that cannot change federated content.
--
-- MEASURED (2026-11-06, production source): 2,382,263 journal events for 31,730 live
-- memories -- 75 events per memory. A fresh replica that replays that journal
-- in pages of 100 at ~0.25 s per page needs ~1.6 hours to catch up, and looks
-- stuck because most of those events re-deliver a row it already has. See
-- docs/FEDERATION_JOURNAL.md for the cause breakdown.
--
-- The dominant cause is the recall bookkeeping write:
-- ``bump_recall_and_get_memory`` / ``bump_recall_counts``
-- (mnemos/persistence/*.py) run
--
--     UPDATE memories SET recall_count = recall_count + 1,
--                         last_recalled_at = NOW()
--
-- 0068 dropped ``updated`` from the change predicate but left ``metadata``
-- and never listed these two columns. They are not part of the export
-- predicate (``eligible_for_federation`` does not read them) and the
-- current-state resolver does not put them on the wire, so an event appended
-- for them can only re-deliver the row's unchanged content.
--
-- What changes here, on all five dialect families:
--
--   * the change predicate is annotated column-by-column: CONTENT columns
--     (the ones the feed exports -- content/verbatim/metadata, scope,
--     lifecycle, the loop guard, the vector) versus METADATA-ONLY columns
--     (``recall_count``/``last_recalled_at``, the recall bookkeeping);
--   * the outbox row records which kind it is (``event_kind``);
--   * METADATA-ONLY events are NOT appended. A write that changed nothing
--     else first absorbs the pending metadata-only rows already queued for
--     that memory and then returns -- so a memory recalled a thousand times
--     between publisher passes leaves zero extra rows, and a memory that is
--     recalled and then genuinely edited leaves exactly one content event
--     carrying its current state.
--
-- This is a schema/trigger-only change: there is no ``seq``-shaped state to
-- migrate, no row is rewritten, and every already-queued row stays exactly as
-- it was. The outbox publisher (mnemos.persistence.federation_journal)
-- coalesces pending rows per memory independently of this file, so a node
-- that has not yet applied 0072 still publishes at most one row per memory
-- per pass.
--
-- The feed's contract does not change: a consumer holding a journal cursor
-- sees the same current-state rows, only fewer of them.

ALTER TABLE federation_changes ADD COLUMN IF NOT EXISTS event_kind VARCHAR(16);
ALTER TABLE federation_changes ALTER COLUMN event_kind SET DEFAULT 'content';

-- "The newest not-yet-published row for this memory" is read by the triggers
-- below; idx_fed_changes_memory_seq is keyed the wrong way round for it.
CREATE INDEX IF NOT EXISTS idx_fed_changes_memory_event ON federation_changes(memory_id, event_id);

CREATE OR REPLACE FUNCTION fed_journal_insert_fn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault')) THEN
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) VALUES(NEW.id,clock_timestamp(),NULL,NULL,0,0,NEW.namespace,NEW.category,CASE WHEN MOD(NEW.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END,'content');
 END IF;
 RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION fed_journal_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
 content_changed BOOLEAN;
BEGIN
 IF ((OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault')) OR (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault'))) AND (OLD.federation_source::text IS DISTINCT FROM NEW.federation_source::text OR OLD.content::text IS DISTINCT FROM NEW.content::text OR OLD.category::text IS DISTINCT FROM NEW.category::text OR OLD.namespace::text IS DISTINCT FROM NEW.namespace::text OR OLD.owner_id::text IS DISTINCT FROM NEW.owner_id::text OR OLD.permission_mode::text IS DISTINCT FROM NEW.permission_mode::text OR OLD.deleted_at::text IS DISTINCT FROM NEW.deleted_at::text OR OLD.archived_at::text IS DISTINCT FROM NEW.archived_at::text OR OLD.consolidated_into::text IS DISTINCT FROM NEW.consolidated_into::text OR OLD.embedding::text IS DISTINCT FROM NEW.embedding::text OR OLD.metadata::text IS DISTINCT FROM NEW.metadata::text OR OLD.verbatim_content::text IS DISTINCT FROM NEW.verbatim_content::text OR OLD.recall_count::text IS DISTINCT FROM NEW.recall_count::text OR OLD.last_recalled_at::text IS DISTINCT FROM NEW.last_recalled_at::text) THEN
 content_changed := (OLD.federation_source::text IS DISTINCT FROM NEW.federation_source::text OR OLD.content::text IS DISTINCT FROM NEW.content::text OR OLD.category::text IS DISTINCT FROM NEW.category::text OR OLD.namespace::text IS DISTINCT FROM NEW.namespace::text OR OLD.owner_id::text IS DISTINCT FROM NEW.owner_id::text OR OLD.permission_mode::text IS DISTINCT FROM NEW.permission_mode::text OR OLD.deleted_at::text IS DISTINCT FROM NEW.deleted_at::text OR OLD.archived_at::text IS DISTINCT FROM NEW.archived_at::text OR OLD.consolidated_into::text IS DISTINCT FROM NEW.consolidated_into::text OR OLD.embedding::text IS DISTINCT FROM NEW.embedding::text OR OLD.metadata::text IS DISTINCT FROM NEW.metadata::text OR OLD.verbatim_content::text IS DISTINCT FROM NEW.verbatim_content::text);
 IF content_changed THEN
  INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) VALUES(NEW.id,clock_timestamp(),OLD.namespace,OLD.category,CASE WHEN MOD(OLD.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NEW.namespace,NEW.category,CASE WHEN MOD(NEW.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END,'content');
 ELSE
  DELETE FROM federation_changes WHERE memory_id = NEW.id AND seq IS NULL AND event_kind = 'metadata';
 END IF;
 END IF;
 RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION fed_journal_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault')) THEN
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) VALUES(OLD.id,clock_timestamp(),OLD.namespace,OLD.category,CASE WHEN MOD(OLD.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NULL,NULL,0,0,'content');
 END IF;
 RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS fed_journal_insert ON memories;
CREATE TRIGGER fed_journal_insert AFTER INSERT ON memories FOR EACH ROW EXECUTE FUNCTION fed_journal_insert_fn();
DROP TRIGGER IF EXISTS fed_journal_update ON memories;
CREATE TRIGGER fed_journal_update AFTER UPDATE ON memories FOR EACH ROW EXECUTE FUNCTION fed_journal_update_fn();
DROP TRIGGER IF EXISTS fed_journal_delete ON memories;
CREATE TRIGGER fed_journal_delete AFTER DELETE ON memories FOR EACH ROW EXECUTE FUNCTION fed_journal_delete_fn();
