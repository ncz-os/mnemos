-- 0072: do not journal writes that cannot change federated content (Db2).
-- Mirror of migrations/0072_federation_journal_metadata_only.sql; see that
-- file and docs/FEDERATION_JOURNAL.md for the rationale and the measured
-- cause breakdown.
--
-- Db2 has no ELSIF: the split is computed into two INTEGER flags first and
-- branched with IF / ELSEIF. The COMPOUND statement is closed with END@ (no
-- trailing slash-protocol terminator).

ALTER TABLE federation_changes ADD COLUMN event_kind VARCHAR(16) DEFAULT 'content'@
CREATE INDEX idx_fed_changes_memory_event ON federation_changes(memory_id, event_id)@

CREATE OR REPLACE TRIGGER fed_journal_update AFTER UPDATE OF federation_source,content,category,namespace,owner_id,permission_mode,deleted_at,archived_at,consolidated_into,embedding,metadata,verbatim_content,recall_count,last_recalled_at ON memories REFERENCING OLD AS o NEW AS n FOR EACH ROW MODE DB2SQL
BEGIN ATOMIC
 DECLARE v_is_content INTEGER;
 DECLARE v_content INTEGER;
 DECLARE v_metadata INTEGER;
 SET v_content = CASE WHEN (o.federation_source IS NULL AND (o.namespace IS NULL OR o.namespace<>'vault') OR n.federation_source IS NULL AND (n.namespace IS NULL OR n.namespace<>'vault'))
                       AND (NOT (o.federation_source <=> n.federation_source)
                         OR NOT (o.category <=> n.category)
                         OR NOT (o.namespace <=> n.namespace)
                         OR NOT (o.owner_id <=> n.owner_id)
                         OR NOT (o.permission_mode <=> n.permission_mode)
                         OR NOT (o.deleted_at <=> n.deleted_at)
                         OR NOT (o.archived_at <=> n.archived_at)
                         OR NOT (o.consolidated_into <=> n.consolidated_into)
                         OR NOT (o.content <=> n.content)
                         OR NOT (o.metadata <=> n.metadata)
                         OR NOT (o.verbatim_content <=> n.verbatim_content)
                         OR NOT (o.embedding <=> n.embedding))
                      THEN 1 ELSE 0 END;
 SET v_metadata = CASE WHEN (o.federation_source IS NULL AND (o.namespace IS NULL OR o.namespace<>'vault') OR n.federation_source IS NULL AND (n.namespace IS NULL OR n.namespace<>'vault'))
                        AND v_content = 0
                        AND (NOT (o.recall_count <=> n.recall_count) OR NOT (o.last_recalled_at <=> n.last_recalled_at))
                       THEN 1 ELSE 0 END;
 SET v_is_content = CASE WHEN v_content = 1 THEN 1 ELSE 0 END;

 IF v_is_content = 1 THEN
  -- A content event carries the current state, which already supersedes any
  -- pending metadata-only row for this memory: publishing both would deliver
  -- the same row twice. Absorb first, then append the single content event.
  DELETE FROM federation_changes WHERE seq IS NULL AND event_kind = 'metadata' AND memory_id = n.id;
  INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind)
   SELECT n.id,CURRENT_TIMESTAMP - CURRENT_TIMEZONE,o.namespace,o.category,CASE WHEN MOD(o.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN o.federation_source IS NULL AND (o.namespace IS NULL OR o.namespace<>'vault') AND o.deleted_at IS NULL AND o.archived_at IS NULL AND o.consolidated_into IS NULL THEN 1 ELSE 0 END,n.namespace,n.category,CASE WHEN MOD(n.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN n.federation_source IS NULL AND (n.namespace IS NULL OR n.namespace<>'vault') AND n.deleted_at IS NULL AND n.archived_at IS NULL AND n.consolidated_into IS NULL THEN 1 ELSE 0 END,'content'
   FROM SYSIBM.SYSDUMMY1;
 ELSEIF v_metadata = 1 THEN
  DELETE FROM federation_changes WHERE seq IS NULL AND event_kind = 'metadata' AND memory_id = n.id;
 END IF;
END@
