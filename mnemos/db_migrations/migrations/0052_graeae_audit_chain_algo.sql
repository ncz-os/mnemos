-- Version the GRAEAE audit-chain algorithm.
--
-- The writers signed each link with an unkeyed sha256(prev + prompt + response)
-- while the verifier expected a keyed HMAC over eight ordered fields, so every
-- row ever written failed verification. Fixing the writer alone is not enough:
-- rows already on disk were signed with the old algorithm, and re-signing them
-- would make them verify only by rewriting a log whose entire purpose is to be
-- unrewritable.
--
-- So each row records the algorithm that signed it. This migration is a pure
-- METADATA BACKFILL -- it does not touch a single chain_hash. Existing rows are
-- labelled sha256-v1 and keep their original signatures; new rows are written
-- as hmac-v2.
ALTER TABLE graeae_audit_log ADD COLUMN IF NOT EXISTS chain_algo VARCHAR(32);
UPDATE graeae_audit_log SET chain_algo = 'sha256-v1' WHERE chain_algo IS NULL;
