-- migration: 0023_hive_messages
-- target:    IBM Db2 12.1.5

BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE '
    CREATE TABLE hive_messages (
      id           VARCHAR(64)   NOT NULL,
      from_urn     VARCHAR(256)  NOT NULL,
      to_urn       VARCHAR(256),
      in_reply_to  VARCHAR(64),
      topic        VARCHAR(128)  NOT NULL,
      payload      CLOB(2M) INLINE LENGTH 4096
        CHECK (payload IS JSON FORMAT JSON STRICT) NOT NULL,
      ts           DOUBLE NOT NULL,
      CONSTRAINT pk_hive_messages PRIMARY KEY (id)
    )';
END%

BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_messages_to    ON hive_messages(to_urn)';
END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_messages_topic ON hive_messages(topic)';
END%
BEGIN DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
  EXECUTE IMMEDIATE 'CREATE INDEX ix_hive_messages_ts    ON hive_messages(ts DESC)';
END%
