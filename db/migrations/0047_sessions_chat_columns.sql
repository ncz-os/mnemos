-- 0047_sessions_chat_columns.sql — PostgreSQL numbered parity anchor.
-- The chat-session columns on sessions/session_messages ship via the named
-- migrations_v2_sessions.sql sequence; this file exists only to satisfy the
-- pg/oracle/db2 migration-parity contract (the Oracle/Db2 siblings add the
-- columns their minimal numbered core schema lacked).
SELECT 1;
