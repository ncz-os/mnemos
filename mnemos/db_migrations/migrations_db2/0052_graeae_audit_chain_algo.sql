--#SET TERMINATOR @
-- Version the GRAEAE audit-chain algorithm (Db2). Pure metadata backfill:
-- existing rows keep their original chain_hash and are labelled with the
-- algorithm that produced them. See the PostgreSQL file for the rationale.
ALTER TABLE graeae_audit_log ADD COLUMN chain_algo VARCHAR(32)@
UPDATE graeae_audit_log SET chain_algo = 'sha256-v1' WHERE chain_algo IS NULL@
