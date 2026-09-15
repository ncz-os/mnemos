-- MariaDB stores canonical vectors outside memories. Cascade deletes are
-- covered by the memories deletion trigger; explicit vector mutations append events.
DROP TRIGGER IF EXISTS fed_embedding_insert;
CREATE TRIGGER fed_embedding_insert AFTER INSERT ON memory_embeddings FOR EACH ROW
INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable)
 SELECT m.id,UTC_TIMESTAMP(6),m.namespace,m.category,
 CASE WHEN MOD(m.permission_mode,10)>=4 THEN 1 ELSE 0 END,1,
 m.namespace,m.category,CASE WHEN MOD(m.permission_mode,10)>=4 THEN 1 ELSE 0 END,1
 FROM memories m WHERE m.id=NEW.memory_id AND m.federation_source IS NULL
 AND m.namespace<>'vault' AND m.deleted_at IS NULL AND m.archived_at IS NULL
 AND m.consolidated_into IS NULL;
DROP TRIGGER IF EXISTS fed_embedding_update;
CREATE TRIGGER fed_embedding_update AFTER UPDATE ON memory_embeddings FOR EACH ROW
INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable)
 SELECT m.id,UTC_TIMESTAMP(6),m.namespace,m.category,
 CASE WHEN MOD(m.permission_mode,10)>=4 THEN 1 ELSE 0 END,1,
 m.namespace,m.category,CASE WHEN MOD(m.permission_mode,10)>=4 THEN 1 ELSE 0 END,1
 FROM memories m WHERE m.id=NEW.memory_id AND m.federation_source IS NULL
 AND m.namespace<>'vault' AND m.deleted_at IS NULL AND m.archived_at IS NULL
 AND m.consolidated_into IS NULL;
DROP TRIGGER IF EXISTS fed_embedding_delete;
CREATE TRIGGER fed_embedding_delete AFTER DELETE ON memory_embeddings FOR EACH ROW
INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable)
 SELECT m.id,UTC_TIMESTAMP(6),m.namespace,m.category,
 CASE WHEN MOD(m.permission_mode,10)>=4 THEN 1 ELSE 0 END,1,
 m.namespace,m.category,CASE WHEN MOD(m.permission_mode,10)>=4 THEN 1 ELSE 0 END,1
 FROM memories m WHERE m.id=OLD.memory_id AND m.federation_source IS NULL
 AND m.namespace<>'vault' AND m.deleted_at IS NULL AND m.archived_at IS NULL
 AND m.consolidated_into IS NULL;
