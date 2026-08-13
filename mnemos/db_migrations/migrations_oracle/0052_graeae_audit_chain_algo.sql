-- Version the GRAEAE audit-chain algorithm (Oracle). Pure metadata backfill:
-- existing rows keep their original chain_hash and are labelled with the
-- algorithm that produced them. See the PostgreSQL file for the rationale.
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'GRAEAE_AUDIT_LOG' AND column_name = 'CHAIN_ALGO';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE graeae_audit_log ADD (chain_algo VARCHAR2(32))';
  END IF;
  EXECUTE IMMEDIATE
    'UPDATE graeae_audit_log SET chain_algo = ''sha256-v1'' WHERE chain_algo IS NULL';
END;
/
