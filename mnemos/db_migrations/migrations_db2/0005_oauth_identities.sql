--#SET TERMINATOR @
-- 0005_oauth_identities.sql — Db2 12.1.5 (Oracle Compat) port.
--
-- Canonical oauth_identities matches PostgreSQL: (id, user_id, provider,
-- external_id, email, display_name, raw_claims, last_login_at, created) with
-- UNIQUE(provider, external_id). Db2Backend inherits the Oracle OAuth methods,
-- so the shape must match. Earlier revisions created a divergent shape
-- (provider_id / profile). The reconcile block mirrors the Oracle sibling: the
-- dead divergent table (has PROVIDER_ID, lacks PROVIDER) is dropped when empty
-- or renamed aside when not (no data loss); a canonical table is untouched. The
-- CONTINUE HANDLER on SQLSTATE 42710 makes the CREATE idempotent on replay.

BEGIN
  FOR c AS
    SELECT COUNT(*) AS has_old FROM SYSCAT.COLUMNS
     WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'OAUTH_IDENTITIES' AND COLNAME = 'PROVIDER_ID'
  DO
    IF c.has_old > 0
       AND NOT EXISTS (SELECT 1 FROM SYSCAT.COLUMNS
                        WHERE TABSCHEMA = CURRENT SCHEMA AND TABNAME = 'OAUTH_IDENTITIES' AND COLNAME = 'PROVIDER')
    THEN
      -- Rename the divergent table aside (never DROP). Done via EXECUTE
      -- IMMEDIATE (dynamic) so this block carries NO static reference to
      -- ``oauth_identities`` — a static ``FROM oauth_identities`` is validated
      -- at compound-statement COMPILE time and would fail with SQL0204N on a
      -- fresh DB where the table does not yet exist. Renaming aside instead of
      -- conditionally dropping keeps the no-data-loss guarantee; an empty
      -- divergent table just lands aside as harmless clutter.
      EXECUTE IMMEDIATE 'RENAME TABLE oauth_identities TO oauth_identities_legacy_0005';
    END IF;
  END FOR;
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE oauth_identities (
      id VARCHAR(36) NOT NULL,
      user_id VARCHAR(100) NOT NULL,
      provider VARCHAR(100) NOT NULL,
      external_id VARCHAR(200) NOT NULL,
      email VARCHAR(200),
      display_name VARCHAR(200),
      raw_claims CLOB(1M),
      last_login_at TIMESTAMP(6),
      created TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
      CONSTRAINT pk_oauth_identities PRIMARY KEY (id)
    )';
END@

-- (provider, external_id) uniqueness as a standalone unique index with a name
-- the divergent 0005 table never used, so a legacy table renamed aside does not
-- collide with this canonical object (see the Oracle sibling for rationale).
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE UNIQUE INDEX uq_oauth_identity_provider_external ON oauth_identities (provider, external_id)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_oauth_identities_user ON oauth_identities (user_id)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX idx_oauth_identities_email ON oauth_identities (email)';
END@
