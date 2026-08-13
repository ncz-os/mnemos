--#SET TERMINATOR @
ALTER TABLE memory_versions ADD COLUMN group_id VARCHAR(255)@
UPDATE memory_versions mv SET group_id = (SELECT m.group_id FROM memories m WHERE m.id = mv.memory_id)
 WHERE group_id IS NULL@
CREATE INDEX idx_mv_group ON memory_versions(group_id)@
CREATE TRIGGER trg_mv_group_snapshot NO CASCADE BEFORE INSERT ON memory_versions
REFERENCING NEW AS n FOR EACH ROW MODE DB2SQL
WHEN (n.group_id IS NULL)
BEGIN ATOMIC
  SET n.group_id = (SELECT group_id FROM memories WHERE id = n.memory_id);
END@
