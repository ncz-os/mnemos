-- Durable content-free outbox. Publishers assign sequence after mutation commit.
CREATE TABLE IF NOT EXISTS federation_change_clock (id INTEGER PRIMARY KEY, value BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS federation_changes (event_id BIGINT AUTO_INCREMENT PRIMARY KEY, seq BIGINT,memory_id VARCHAR(255) NOT NULL,changed_at DATETIME(6) NOT NULL,old_namespace VARCHAR(255),old_category VARCHAR(255),old_public INTEGER NOT NULL,old_exportable INTEGER NOT NULL,new_namespace VARCHAR(255),new_category VARCHAR(255),new_public INTEGER NOT NULL,new_exportable INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS federation_receive_state (peer_name VARCHAR(255) NOT NULL,remote_id VARCHAR(255) NOT NULL,last_sequence BIGINT,remote_updated DATETIME(6),withdrawn INTEGER NOT NULL,PRIMARY KEY(peer_name,remote_id));
CREATE TABLE IF NOT EXISTS federation_peer_cursors (peer_id VARCHAR(255) PRIMARY KEY,cursor_value VARCHAR(2048),filter_signature VARCHAR(64));
CREATE INDEX idx_fed_changes_memory_seq ON federation_changes(memory_id,seq);

INSERT IGNORE INTO federation_change_clock(id,value) VALUES(1,0);
DROP TRIGGER IF EXISTS fed_journal_insert;
CREATE TRIGGER fed_journal_insert AFTER INSERT ON memories FOR EACH ROW
BEGIN
 IF (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault')) THEN
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable) SELECT NEW.id,UTC_TIMESTAMP(6),NULL,NULL,0,0,NEW.namespace,NEW.category,CASE WHEN MOD(NEW.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END;
 END IF;
END;
DROP TRIGGER IF EXISTS fed_journal_update;
CREATE TRIGGER fed_journal_update AFTER UPDATE ON memories FOR EACH ROW
BEGIN
 IF ((OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault')) OR (NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault'))) AND (NOT (OLD.federation_source <=> NEW.federation_source) OR NOT (OLD.content <=> NEW.content) OR NOT (OLD.category <=> NEW.category) OR NOT (OLD.subcategory <=> NEW.subcategory) OR NOT (OLD.namespace <=> NEW.namespace) OR NOT (OLD.owner_id <=> NEW.owner_id) OR NOT (OLD.permission_mode <=> NEW.permission_mode) OR NOT (OLD.deleted_at <=> NEW.deleted_at) OR NOT (OLD.archived_at <=> NEW.archived_at) OR NOT (OLD.consolidated_into <=> NEW.consolidated_into) OR NOT (OLD.updated <=> NEW.updated) OR NOT (OLD.embedding <=> NEW.embedding) OR NOT (OLD.metadata <=> NEW.metadata) OR NOT (OLD.verbatim_content <=> NEW.verbatim_content) OR NOT (OLD.source_model <=> NEW.source_model) OR NOT (OLD.source_provider <=> NEW.source_provider) OR NOT (OLD.source_agent <=> NEW.source_agent) OR NOT (OLD.source_session <=> NEW.source_session)) THEN
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable) SELECT NEW.id,UTC_TIMESTAMP(6),OLD.namespace,OLD.category,CASE WHEN MOD(OLD.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NEW.namespace,NEW.category,CASE WHEN MOD(NEW.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN NEW.federation_source IS NULL AND (NEW.namespace IS NULL OR NEW.namespace<>'vault') AND NEW.deleted_at IS NULL AND NEW.archived_at IS NULL AND NEW.consolidated_into IS NULL THEN 1 ELSE 0 END;
 END IF;
END;
DROP TRIGGER IF EXISTS fed_journal_delete;
CREATE TRIGGER fed_journal_delete AFTER DELETE ON memories FOR EACH ROW
BEGIN
 IF (OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault')) THEN
 INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable) SELECT OLD.id,UTC_TIMESTAMP(6),OLD.namespace,OLD.category,CASE WHEN MOD(OLD.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN OLD.federation_source IS NULL AND (OLD.namespace IS NULL OR OLD.namespace<>'vault') AND OLD.deleted_at IS NULL AND OLD.archived_at IS NULL AND OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,NULL,NULL,0,0;
 END IF;
END;
INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable) SELECT m.id,UTC_TIMESTAMP(6),NULL,NULL,0,0,m.namespace,m.category,CASE WHEN MOD(m.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN m.federation_source IS NULL AND (m.namespace IS NULL OR m.namespace<>'vault') AND m.deleted_at IS NULL AND m.archived_at IS NULL AND m.consolidated_into IS NULL THEN 1 ELSE 0 END FROM memories m WHERE (m.federation_source IS NULL AND (m.namespace IS NULL OR m.namespace<>'vault')) AND NOT EXISTS(SELECT 1 FROM federation_changes e WHERE e.memory_id=m.id);
CREATE INDEX idx_fed_changes_pending ON federation_changes(seq,event_id);
