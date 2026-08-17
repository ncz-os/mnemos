ALTER TABLE memory_versions ADD COLUMN group_id TEXT;
UPDATE memory_versions SET group_id = (SELECT group_id FROM memories WHERE memories.id = memory_versions.memory_id)
 WHERE group_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_mv_group ON memory_versions(group_id);
CREATE TRIGGER IF NOT EXISTS trg_memory_version_group_snapshot
AFTER INSERT ON memory_versions WHEN NEW.group_id IS NULL
BEGIN
  UPDATE memory_versions SET group_id = (SELECT group_id FROM memories WHERE id = NEW.memory_id)
   WHERE id = NEW.id;
END;
