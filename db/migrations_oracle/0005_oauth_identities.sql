-- 0005_oauth_identities.sql — Oracle 23ai/26ai port for MNEMOS parity.
--
-- Canonical oauth_identities matches PostgreSQL (db/migrations_v3_oauth.sql):
-- (id, user_id, provider, external_id, email, display_name, raw_claims,
-- last_login_at, created) with UNIQUE(provider, external_id). The
-- OracleOAuthRepository methods (provision_or_link_user, get_identity_for_session)
-- already reference these canonical columns.
--
-- Earlier revisions of this file created a divergent shape
-- (provider_id / profile, UNIQUE(provider_id, external_id)) that the repository
-- never matched. The guard reconciles ONLY that dead divergent table (has
-- PROVIDER_ID, lacks PROVIDER): drops it when empty, renames it aside when not
-- (no data loss). A canonical table (has PROVIDER) is never touched. Safe to
-- replay. raw_claims is a plain CLOB (JSON text; no IS JSON) for Db2-translatable
-- inheritance and string parity, matching the journal port.

DECLARE
    v_old  NUMBER;
    v_new  NUMBER;
    v_rows NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_old
      FROM user_tab_columns
     WHERE table_name = 'OAUTH_IDENTITIES' AND column_name = 'PROVIDER_ID';
    SELECT COUNT(*) INTO v_new
      FROM user_tab_columns
     WHERE table_name = 'OAUTH_IDENTITIES' AND column_name = 'PROVIDER';
    IF v_old > 0 AND v_new = 0 THEN
        EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM oauth_identities' INTO v_rows;
        IF v_rows = 0 THEN
            EXECUTE IMMEDIATE 'DROP TABLE oauth_identities CASCADE CONSTRAINTS';
        ELSE
            EXECUTE IMMEDIATE 'RENAME oauth_identities TO oauth_identities_legacy_0005';
        END IF;
    END IF;
END;
/

CREATE TABLE IF NOT EXISTS oauth_identities (
    id VARCHAR2(36) PRIMARY KEY,
    user_id VARCHAR2(100) NOT NULL,
    provider VARCHAR2(100) NOT NULL,
    external_id VARCHAR2(200) NOT NULL,
    email VARCHAR2(200),
    display_name VARCHAR2(200),
    raw_claims CLOB,
    last_login_at TIMESTAMP WITH TIME ZONE,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

-- The (provider, external_id) uniqueness is a standalone unique index with a
-- name the divergent 0005 table never used. A legacy table renamed aside (the
-- non-empty reconcile branch) keeps its old object names (uq_oauth_identity,
-- idx_oauth_identities_user/provider); using distinct canonical names avoids a
-- duplicate-object-name collision that would otherwise abort this CREATE.
CREATE UNIQUE INDEX IF NOT EXISTS uq_oauth_identity_provider_external
    ON oauth_identities (provider, external_id);
CREATE INDEX IF NOT EXISTS idx_oauth_identities_user ON oauth_identities (user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_identities_email ON oauth_identities (email);
