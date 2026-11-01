-- F-4: narrow the federation journal update trigger.
--
-- The 0062 predicate fires on every UPDATE that touches ``subcategory``,
-- ``updated``, ``embedding``, ``metadata`` or any ``source_*`` column. Two of
-- those are pure bookkeeping and one fires on a no-op:
--
--   * ``updated``          -- bumped by every write, changes nothing the
--                             feed exports on its own;
--   * ``subcategory`` and the ``source_*`` columns -- derivative metadata: not
--                             part of the fed scope predicate, and the
--                             current-state resolver does not export them, so
--                             an event appended for them alone carries no
--                             information the feed can act on;
--   * a same-value UPDATE  -- every column compared with ``IS NOT`` meant a
--                             touch that only re-stamped ``updated``
--                             appended an event.
--
-- ``embedding`` and ``metadata`` still fire: the journal cannot know a
-- consumer's scope at trigger time, so a peer pulling with
-- ``copy_embeddings=1`` must still be journaled when the vector changes.
-- This narrows the no-op and bookkeeping re-fires, not derivative-only
-- updates in general -- narrowing ``embedding`` would silently stop vector
-- replication.
--
-- ``deleted_at``/``archived_at``/``consolidated_into`` stay: they are the
-- withdrawal/tombstone signal. ``federation_source`` stays: it is the
-- loop-guard transition. ``namespace``/``category``/``permission_mode``/
-- ``owner_id`` stay: they gate export scope.
--
-- This is a schema change gated on the row being created; the runtime
-- behaviour is gated separately by MNEMOS_FEDERATION_JOURNAL_COMPACTION,
-- which governs compaction (draining dead events), not the trigger.

DROP TRIGGER IF EXISTS fed_journal_update;
CREATE TRIGGER fed_journal_update AFTER UPDATE ON memories
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
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable) SELECT NEW.id,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),OLD.namespace,OLD.category,CASE WHEN (OLD.permission_mode % 10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NEW.namespace,NEW.category,CASE WHEN (NEW.permission_mode % 10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END;
END;

CREATE INDEX IF NOT EXISTS idx_fed_changes_memory_changed ON federation_changes(memory_id, changed_at);
