--#SET TERMINATOR @
-- Cross-backend supersession parity: add memories.consolidated_into if absent.
-- DB2 has no ADD COLUMN IF NOT EXISTS; guard on syscat.columns.
BEGIN
  IF (SELECT COUNT(*) FROM syscat.columns
        WHERE tabname = 'MEMORIES' AND colname = 'CONSOLIDATED_INTO') = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE memories ADD COLUMN consolidated_into VARCHAR(64)';
  END IF;
END@
