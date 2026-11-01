-- F-4: narrow the federation journal update trigger (see the Postgres 0068).
-- ``subcategory``, ``updated`` and the ``source_*`` bookkeeping columns no
-- longer fire the journal; ``embedding``/``metadata`` still do (a
-- copy_embeddings peer receives them).
--
-- Value comparison, not UPDATING('col'): UPDATING is true whenever the column
-- is in the SET list, even when the value is unchanged, so a same-value write
-- appended an event here that the SQLite (IS NOT), Postgres (IS DISTINCT
-- FROM) and MySQL/MariaDB (NOT <=>) twins all suppress. Each column is
-- compared null-safely: ``<>`` is NULL when either side is NULL, so both
-- NULL-transition arms are spelled out. CLOB columns go through
-- DBMS_LOB.COMPARE. VECTOR has no comparison operator, so the embedding is
-- compared in SQL with an exact EUCLIDEAN distance, and only when nothing
-- cheaper already changed and the column was in the SET list at all.
CREATE OR REPLACE TRIGGER fed_journal_update AFTER UPDATE ON memories FOR EACH ROW
DECLARE
 v_changed NUMBER := 0;
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
   v_changed := 1;
  ELSIF UPDATING('embedding') THEN
   SELECT CASE WHEN :OLD.embedding IS NULL AND :NEW.embedding IS NOT NULL THEN 1 WHEN :OLD.embedding IS NOT NULL AND :NEW.embedding IS NULL THEN 1 WHEN VECTOR_DISTANCE(:OLD.embedding, :NEW.embedding, EUCLIDEAN) <> 0 THEN 1 ELSE 0 END INTO v_changed FROM dual;
  END IF;
  IF v_changed = 1 THEN
   INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,old_public,old_exportable,new_namespace,new_category,new_public,new_exportable) VALUES(:NEW.id,SYS_EXTRACT_UTC(SYSTIMESTAMP),:OLD.namespace,:OLD.category,CASE WHEN MOD(:OLD.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN :OLD.federation_source IS NULL AND (:OLD.namespace IS NULL OR :OLD.namespace<>'vault') AND :OLD.deleted_at IS NULL AND :OLD.archived_at IS NULL AND :OLD.consolidated_into IS NULL THEN 1 ELSE 0 END,:NEW.namespace,:NEW.category,CASE WHEN MOD(:NEW.permission_mode,10)>=4 THEN 1 ELSE 0 END,CASE WHEN :NEW.federation_source IS NULL AND (:NEW.namespace IS NULL OR :NEW.namespace<>'vault') AND :NEW.deleted_at IS NULL AND :NEW.archived_at IS NULL AND :NEW.consolidated_into IS NULL THEN 1 ELSE 0 END);
  END IF;
 END IF;
END;
/
