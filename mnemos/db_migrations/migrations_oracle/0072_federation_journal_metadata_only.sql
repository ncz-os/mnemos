-- 0072: do not journal writes that cannot change federated content (Oracle).
-- Mirror of migrations/0072_federation_journal_metadata_only.sql; see that
-- file and docs/FEDERATION_JOURNAL.md for the rationale and the measured
-- cause breakdown.
--
-- The CONTENT / METADATA-ONLY split is a value comparison like every other
-- column here (see 0068: UPDATING() is true for a same-value write, so it
-- cannot be used to decide anything). ``recall_count``/``last_recalled_at``
-- are compared null-safely and are responsible only for the METADATA-ONLY
-- branch, which DELETEs any queued metadata-only rows for the memory and
-- appends nothing.
--
-- The METADATA-ONLY branch also covers a statement that did not name any
-- CONTENT column in its SET list at all: Oracle updates EVERY row matched by a
-- statement's WHERE clause whether or not its SET list touches that row's
-- content, so a bulk bookkeeping UPDATE re-fires this trigger on rows whose
-- content columns are untouched. Oracle fires no trigger at all for a statement
-- that omits every journaled column, so the branch must absorb the queued rows
-- itself rather than rely on staying silent -- otherwise a memory recalled
-- between a content event and the publisher's pass keeps that content event
-- alive as a false journal entry.

ALTER TABLE federation_changes ADD event_kind VARCHAR2(16) DEFAULT 'content';
CREATE INDEX idx_fed_changes_memory_event ON federation_changes(memory_id, event_id);

CREATE OR REPLACE TRIGGER fed_journal_update AFTER UPDATE ON memories FOR EACH ROW
DECLARE
 v_content NUMBER := 0;
 v_metadata NUMBER := 0;
BEGIN
 IF ((:OLD.federation_source IS NULL AND (:OLD.namespace IS NULL OR :OLD.namespace<>'vault')) OR (:NEW.federation_source IS NULL AND (:NEW.namespace IS NULL OR :NEW.namespace<>'vault'))) THEN
  IF (:OLD.federation_source <> :NEW.federation_source OR (:OLD.federation_source IS NULL AND :NEW.federation_source IS NOT NULL) OR (:OLD.federation_source IS NOT NULL AND :NEW.federation_source IS NULL))
   OR (:OLD.category <> :NEW.category OR (:OLD.category IS NULL AND :NEW.category IS NOT NULL) OR (:OLD.category IS NOT NULL AND :NEW.category IS NULL))
   OR (:OLD.namespace <> :NEW.namespace OR (:OLD.namespace IS NULL AND :NEW.namespace IS NOT NULL) OR (:OLD.namespace IS NOT NULL AND :NEW.namespace IS NULL))
   OR (:OLD.owner_id <> :NEW.owner_id OR (:OLD.owner_id IS NULL AND :NEW.owner_id IS NOT NULL) OR (:OLD.owner_id IS NOT NULL AND :NEW.owner_id IS NULL))
   OR (:OLD.permission_mode <> :NEW.permission_mode OR (:OLD.permission_mode IS NULL AND :NEW.permission_mode IS NOT NULL) OR (:OLD.permission_mode IS NOT NULL AND :NEW.permission_mode IS NULL))
   OR (:OLD.deleted_at <> :NEW.deleted_at OR (:OLD.deleted_at IS NULL AND :NEW.deleted_at IS NOT NULL) OR (:OLD.deleted_at IS NOT NULL AND :NEW.deleted_at IS NULL))
   OR (:OLD.archived_at <> :NEW.archived_at OR (:OLD.archived_at IS NULL AND :NEW.archived_at IS NOT NULL) OR (:OLD.archived_at IS NOT NULL AND :NEW.archived_at IS NULL))
   OR (:OLD.consolidated_into <> :NEW.consolidated_into OR (:OLD.consolidated_into IS NULL AND :NEW.consolidated_into IS NOT NULL) OR (:OLD.consolidated_into IS NOT NULL AND :NEW.consolidated_into IS NULL))
   OR (DBMS_LOB.COMPARE(:OLD.content, :NEW.content) <> 0 OR (:OLD.content IS NULL AND :NEW.content IS NOT NULL) OR (:OLD.content IS NOT NULL AND :NEW.content IS NULL))
   OR (DBMS_LOB.COMPARE(:OLD.metadata, :NEW.metadata) <> 0 OR (:OLD.metadata IS NULL AND :NEW.metadata IS NOT NULL) OR (:OLD.metadata IS NOT NULL AND :NEW.metadata IS NULL))
   OR (DBMS_LOB.COMPARE(:OLD.verbatim_content, :NEW.verbatim_content) <> 0 OR (:OLD.verbatim_content IS NULL AND :NEW.verbatim_content IS NOT NULL) OR (:OLD.verbatim_content IS NOT NULL AND :NEW.verbatim_content IS NULL)) THEN
   v_content := 1;
  ELSIF UPDATING('embedding') THEN
   SELECT CASE WHEN :OLD.embedding IS NULL AND :NEW.embedding IS NOT NULL THEN 1 WHEN :OLD.embedding IS NOT NULL AND :NEW.embedding IS NULL THEN 1 WHEN VECTOR_DISTANCE(:OLD.embedding, :NEW.embedding, EUCLIDEAN) <> 0 THEN 1 ELSE 0 END INTO v_content FROM dual;
  ELSIF (:OLD.recall_count <> :NEW.recall_count OR (:OLD.recall_count IS NULL AND :NEW.recall_count IS NOT NULL) OR (:OLD.recall_count IS NOT NULL AND :NEW.recall_count IS NULL))
   OR (:OLD.last_recalled_at <> :NEW.last_recalled_at OR (:OLD.last_recalled_at IS NULL AND :NEW.last_recalled_at IS NOT NULL) OR (:OLD.last_recalled_at IS NOT NULL AND :NEW.last_recalled_at IS NULL))
   OR (NOT (UPDATING('federation_source') OR UPDATING('content') OR UPDATING('category') OR UPDATING('namespace') OR UPDATING('owner_id') OR UPDATING('permission_mode') OR UPDATING('deleted_at') OR UPDATING('archived_at') OR UPDATING('consolidated_into') OR UPDATING('embedding') OR UPDATING('metadata') OR UPDATING('verbatim_content'))) THEN
   v_metadata := 1;
  END IF;
  IF v_content = 1 THEN
   INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable,event_kind) VALUES(:NEW.id,SYS_EXTRACT_UTC(SYSTIMESTAMP),:OLD.namespace,:OLD.category,CASE WHEN MOD(:OLD.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN :OLD.federation_source IS NULL AND (:OLD.namespace IS NULL OR :OLD.namespace<>'vault') AND :OLD.deleted_at IS NULL AND :OLD.archived_at IS NULL AND :OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,:NEW.namespace,:NEW.category,CASE WHEN MOD(:NEW.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN :NEW.federation_source IS NULL AND (:NEW.namespace IS NULL OR :NEW.namespace<>'vault') AND :NEW.deleted_at IS NULL AND :NEW.archived_at IS NULL AND :NEW.consolidated_into IS NULL THEN 1 ELSE 0 END,'content');
  ELSIF v_metadata = 1 THEN
   DELETE FROM federation_changes WHERE seq IS NULL AND event_kind = 'metadata' AND memory_id = :NEW.id;
  END IF;
 END IF;
END;
/
