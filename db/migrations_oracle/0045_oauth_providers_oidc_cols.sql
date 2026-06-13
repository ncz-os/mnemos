-- 0045_oauth_providers_oidc_cols.sql — OIDC columns on oauth_providers, with the
-- client_secret stored ENCRYPTED at rest (GRAEAE 2026-06-13 + vendor best
-- practice: never plaintext; transparent DB encryption still exposes plaintext to
-- any DB user, so encrypt at the app layer). get_provider decrypts transiently for
-- build_client. The legacy token-hash columns (client_secret_hash, auth_url) are
-- retained but unused.
ALTER TABLE oauth_providers ADD (
  kind              VARCHAR2(50) DEFAULT 'oidc',
  issuer_url        VARCHAR2(500),
  display_name      VARCHAR2(255),
  authorize_url     VARCHAR2(500),
  userinfo_url      VARCHAR2(500),
  "scope"           VARCHAR2(500),
  client_secret_enc CLOB
);
