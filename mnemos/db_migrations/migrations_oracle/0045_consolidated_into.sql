-- Add memories.consolidated_into for cross-backend supersession parity.
-- All other backends (mysql/postgres/sqlite/db2) define this column; Oracle
-- lacked it, which broke the federation feed (ORA-00904) once feed/by-id
-- eligibility + boost rerank began referencing m.consolidated_into.
-- Idempotent guard: skip if the column already exists.
DECLARE
  n NUMBER;
BEGIN
  SELECT COUNT(*) INTO n FROM user_tab_columns
   WHERE table_name = 'MEMORIES' AND column_name = 'CONSOLIDATED_INTO';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE memories ADD (consolidated_into VARCHAR2(64))';
  END IF;
END;
/
