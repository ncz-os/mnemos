-- migration: 0003_hive_messages
-- target:    Oracle 23ai PDB ORCLPDB1
-- schema:    HIVE_MIND
-- purpose:   Hive Mind agent-to-agent messages. SQLite -> Oracle port.
--
-- Idempotency: guarded by USER_TABLES.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'HIVE_MESSAGES';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hive_messages (
        id           VARCHAR2(64)   NOT NULL,
        from_urn     VARCHAR2(256)  NOT NULL,
        to_urn       VARCHAR2(256),
        in_reply_to  VARCHAR2(64),
        topic        VARCHAR2(128)  NOT NULL,
        payload      JSON           NOT NULL,
        ts           NUMBER         NOT NULL,
        CONSTRAINT pk_hive_messages PRIMARY KEY (id)
      )
    ]';
  END IF;
END;
/

DECLARE
  PROCEDURE create_index(p_name VARCHAR2, p_ddl VARCHAR2) IS
    v_n NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v_n FROM user_indexes WHERE index_name = p_name;
    IF v_n = 0 THEN EXECUTE IMMEDIATE p_ddl; END IF;
  END;
BEGIN
  create_index('IX_HIVE_MESSAGES_TO',    'CREATE INDEX ix_hive_messages_to ON hive_messages(to_urn)');
  create_index('IX_HIVE_MESSAGES_TOPIC', 'CREATE INDEX ix_hive_messages_topic ON hive_messages(topic)');
  create_index('IX_HIVE_MESSAGES_TS',    'CREATE INDEX ix_hive_messages_ts ON hive_messages(ts DESC)');
END;
/

COMMIT;
