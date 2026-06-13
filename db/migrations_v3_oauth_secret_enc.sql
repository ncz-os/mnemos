-- migrations_v3_oauth_secret_enc.sql — OAuth provider client_secret at-rest
-- encryption (GRAEAE 2026-06-13 + vendor best practice; mirrors oracle 0045 /
-- db2 0043). Backward-compatible: get_provider prefers the decrypted
-- client_secret_enc and falls back to the legacy plaintext client_secret for
-- rows not yet backfilled. Operator backfills (encrypt existing secrets) then
-- drops the plaintext client_secret column (Phase 2.3-2.5).
ALTER TABLE oauth_providers ADD COLUMN IF NOT EXISTS client_secret_enc TEXT;
