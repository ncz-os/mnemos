-- Freeze group tenancy on snapshots; ACLs remain normalized in memory_acl and
-- are joined through memory_versions.memory_id.
ALTER TABLE memory_versions ADD COLUMN IF NOT EXISTS group_id TEXT;
UPDATE memory_versions mv SET group_id = m.group_id FROM memories m
 WHERE mv.memory_id = m.id AND mv.group_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_mv_group ON memory_versions(group_id);
CREATE OR REPLACE FUNCTION mnemos_version_group_snapshot() RETURNS trigger AS $$
BEGIN
  IF NEW.group_id IS NULL THEN
    SELECT group_id INTO NEW.group_id FROM memories WHERE id = NEW.memory_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_memory_version_group_snapshot ON memory_versions;
CREATE TRIGGER trg_memory_version_group_snapshot BEFORE INSERT ON memory_versions
FOR EACH ROW EXECUTE FUNCTION mnemos_version_group_snapshot();
