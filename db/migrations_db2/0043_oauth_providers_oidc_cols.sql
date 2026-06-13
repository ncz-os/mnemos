-- 0043_oauth_providers_oidc_cols.sql — OIDC columns on oauth_providers with the
-- client_secret stored ENCRYPTED at rest (GRAEAE 2026-06-13 + vendor best
-- practice; mirrors oracle 0045). get_provider decrypts transiently for
-- build_client. Legacy token-hash columns retained but unused.
ALTER TABLE oauth_providers ADD COLUMN kind VARCHAR(50) DEFAULT 'oidc';
ALTER TABLE oauth_providers ADD COLUMN issuer_url VARCHAR(500);
ALTER TABLE oauth_providers ADD COLUMN display_name VARCHAR(255);
ALTER TABLE oauth_providers ADD COLUMN authorize_url VARCHAR(500);
ALTER TABLE oauth_providers ADD COLUMN userinfo_url VARCHAR(500);
ALTER TABLE oauth_providers ADD COLUMN scope VARCHAR(500);
ALTER TABLE oauth_providers ADD COLUMN client_secret_enc CLOB(1M);

CALL SYSPROC.ADMIN_CMD('REORG TABLE oauth_providers');
