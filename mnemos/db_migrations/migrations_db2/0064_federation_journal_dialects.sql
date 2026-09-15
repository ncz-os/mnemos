-- Repair journal triggers installed by 0062; preserve durable events/cursors.
--#SET TERMINATOR @
CREATE OR REPLACE TRIGGER fed_journal_update AFTER UPDATE OF federation_source,content,category,subcategory,namespace,owner_id,permission_mode,deleted_at,archived_at,consolidated_into,updated,embedding,metadata,verbatim_content,source_model,source_provider,source_agent,source_session ON memories REFERENCING OLD AS o NEW AS n FOR EACH ROW MODE DB2SQL
WHEN ((o.federation_source IS NULL AND (o.namespace IS NULL OR o.namespace<>'vault')) OR (n.federation_source IS NULL AND (n.namespace IS NULL OR n.namespace<>'vault')))
BEGIN ATOMIC
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable) SELECT n.id,CURRENT_TIMESTAMP,o.namespace,o.category,CASE WHEN MOD(o.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN o.federation_source IS NULL AND (o.namespace IS NULL OR o.namespace<>'vault') AND o.deleted_at IS NULL AND o.archived_at IS NULL AND o.consolidated_into IS NULL THEN 1 ELSE 0 END,n.namespace,n.category,CASE WHEN MOD(n.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN n.federation_source IS NULL AND (n.namespace IS NULL OR n.namespace<>'vault') AND n.deleted_at IS NULL AND n.archived_at IS NULL AND n.consolidated_into IS NULL THEN 1 ELSE 0 END FROM SYSIBM.SYSDUMMY1;
END@
