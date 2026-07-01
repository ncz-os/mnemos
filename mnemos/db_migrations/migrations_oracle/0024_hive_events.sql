-- migration: 0004_hive_events
-- target:    Oracle 23ai PDB ORCLPDB1
-- schema:    HIVE_MIND
-- purpose:   Hive Mind event audit log. SQLite -> Oracle port.
--            SQLite used AUTOINCREMENT integer id; Oracle uses identity
--            column for equivalent server-side allocation.
--
-- Idempotency: guarded by USER_TABLES.

DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'HIVE_EVENTS';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hive_events (
        id         NUMBER GENERATED ALWAYS AS IDENTITY (CACHE 100) NOT NULL,
        ts         NUMBER          NOT NULL,
        kind       VARCHAR2(64)    NOT NULL,
        payload    JSON            NOT NULL,
        agent_urn  VARCHAR2(256),
        CONSTRAINT pk_hive_events PRIMARY KEY (id)
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
  create_index('IX_HIVE_EVENTS_TS',      'CREATE INDEX ix_hive_events_ts ON hive_events(ts DESC)');
  create_index('IX_HIVE_EVENTS_KIND',    'CREATE INDEX ix_hive_events_kind ON hive_events(kind)');
  create_index('IX_HIVE_EVENTS_AGENT',   'CREATE INDEX ix_hive_events_agent ON hive_events(agent_urn)');
  create_index('IX_HIVE_EVENTS_KIND_TS', 'CREATE INDEX ix_hive_events_kind_ts ON hive_events(kind, ts DESC)');
END;
/

COMMIT;
