ALTER TABLE memory_versions ADD group_id VARCHAR2(100);
UPDATE memory_versions mv SET group_id = (SELECT m.group_id FROM memories m WHERE m.id = mv.memory_id)
 WHERE group_id IS NULL;
CREATE INDEX idx_mv_group ON memory_versions(group_id);
CREATE OR REPLACE TRIGGER trg_mv_group_snapshot
BEFORE INSERT ON memory_versions FOR EACH ROW
WHEN (NEW.group_id IS NULL)
BEGIN
  SELECT group_id INTO :NEW.group_id FROM memories WHERE id = :NEW.memory_id;
EXCEPTION WHEN NO_DATA_FOUND THEN NULL;
END;
/
