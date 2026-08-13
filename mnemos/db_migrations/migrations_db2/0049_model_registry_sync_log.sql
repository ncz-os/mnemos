--#SET TERMINATOR @
-- Db2 model-provider synchronization audit log. The repository inherits the
-- writer from Oracle, whose compatibility cursor translates its binds and
-- SYSTIMESTAMP expression to Db2-native SQL.
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE model_registry_sync_log (
      id                CHAR(16) FOR BIT DATA NOT NULL,
      provider          VARCHAR(50) NOT NULL,
      synced_at         TIMESTAMP(6) DEFAULT CURRENT TIMESTAMP NOT NULL,
      models_found      INTEGER DEFAULT 0 NOT NULL,
      models_added      INTEGER DEFAULT 0 NOT NULL,
      models_updated    INTEGER DEFAULT 0 NOT NULL,
      models_deprecated INTEGER DEFAULT 0 NOT NULL,
      error             VARCHAR(4000),
      CONSTRAINT pk_model_registry_sync_log PRIMARY KEY (id)
    )';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_model_registry_sync_provider ON model_registry_sync_log(provider)';
END@

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_model_registry_sync_synced_at ON model_registry_sync_log(synced_at DESC)';
END@
