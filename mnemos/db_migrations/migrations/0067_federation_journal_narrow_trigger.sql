-- F-4: narrow the federation journal update trigger.
--
-- Drop ``subcategory``, ``updated`` and the ``source_*`` bookkeeping columns
-- from the change predicate. ``updated`` is re-stamped by every writer, so any
-- touch that merely re-stamps it appended an event no consumer exports.
-- ``subcategory``/``source_*`` are not part of the fed scope predicate.
--
-- ``embedding`` and ``metadata`` stay: a ``copy_embeddings=1`` peer receives
-- the vector, and the trigger cannot see a consumer's scope.
CREATE OR REPLACE FUNCTION fed_journal_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ((OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault')) OR (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault'))) AND (OLD.federation_source::text IS DISTINCT FROM NEW.federation_source::text OR OLD.content::text IS DISTINCT FROM NEW.content::text OR OLD.category::text IS DISTINCT FROM NEW.category::text OR OLD.namespace::text IS DISTINCT FROM NEW.namespace::text OR OLD.owner_id::text IS DISTINCT FROM NEW.owner_id::text OR OLD.permission_mode::text IS DISTINCT FROM NEW.permission_mode::text OR OLD.deleted_at::text IS DISTINCT FROM NEW.deleted_at::text OR OLD.archived_at::text IS DISTINCT FROM NEW.archived_at::text OR OLD.consolidated_into::text IS DISTINCT FROM NEW.consolidated_into::text OR OLD.embedding::text IS DISTINCT FROM NEW.embedding::text OR OLD.metadata::text IS DISTINCT FROM NEW.metadata::text OR OLD.verbatim_content::text IS DISTINCT FROM NEW.verbatim_content::text) THEN
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable) VALUES(NEW.id,clock_timestamp(),OLD.namespace,OLD.category,CASE WHEN MOD(OLD.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NEW.namespace,NEW.category,CASE WHEN MOD(NEW.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END);
 END IF;
 RETURN NULL;
END;
$$;
CREATE INDEX IF NOT EXISTS idx_fed_changes_memory_changed ON federation_changes(memory_id, changed_at);
