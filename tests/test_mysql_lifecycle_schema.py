from __future__ import annotations

from mnemos.persistence import mariadb, mysql


def test_mysql_family_inline_schema_covers_lifecycle_worker_manifest():
    for ddls in (mysql._INIT_DDLS, mariadb._INIT_DDLS):
        schema = "\n".join(ddls).lower()
        for table in (
            "entities",
            "sessions",
            "session_messages",
            "session_memory_injections",
            "memory_branches",
        ):
            start = schema.index(f"create table if not exists {table}")
            end = schema.index("default charset", start)
            assert "deleted_at" in schema[start:end], table

        entities = schema[schema.index("create table if not exists entities") :]
        assert "owner_id" in entities and "namespace" in entities
        sessions = schema[schema.index("create table if not exists sessions") :]
        assert "user_id" in sessions and "namespace" in sessions
