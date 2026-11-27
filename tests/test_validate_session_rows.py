import re
import sqlite3
from types import SimpleNamespace

import pytest

from mnemos.persistence.oracle import OracleSessionsRepository
from mnemos.persistence.db2 import Db2SessionsRepository


class Cursor:
    def __init__(self, conn):
        self.cursor = conn.cursor()

    def execute(self, sql, params=None):
        sql = sql.replace("SYSTIMESTAMP", "CURRENT_TIMESTAMP").replace("TO_CHAR(created_at)", "created_at")

        def _translate_paging(match: re.Match[str]) -> str:
            """Translate Oracle-style FETCH/OFFSET to SQLite LIMIT/OFFSET.

            The named binds are named parameters of the SQLite call, so each
            is replaced with its own bind placeholder (``:__limit`` /
            ``:__offset``). The translation works by bind NAME, not by
            assuming a fixed source order — a reordered bind list can no
            longer produce silently wrong SQL.
            """
            parts = match.groupdict()
            limit_name = parts["limit"].lstrip(":")
            if parts.get("offset") is not None:
                offset_name = parts["offset"].lstrip(":")
                return f"LIMIT :__{limit_name} OFFSET :__{offset_name}"
            return f"LIMIT :__{limit_name}"

        sql = re.sub(
            r"OFFSET (?P<offset>:\w+) ROWS FETCH NEXT (?P<limit>:\w+) ROWS ONLY",
            _translate_paging,
            sql,
        )
        sql = re.sub(r"FETCH FIRST (?P<limit>:\w+) ROWS ONLY", _translate_paging, sql)

        # Re-map translated named parameters so the values stay bound by name.
        p = dict(params or {})
        remapped = {}
        for key, value in p.items():
            if "__" + key in sql:
                remapped[f"__{key}"] = value
            else:
                remapped[key] = value
        return self.cursor.execute(sql, remapped)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_type", [OracleSessionsRepository, Db2SessionsRepository])
async def test_validate_session_roundtrip(repo_type):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, namespace TEXT, model TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_activity TEXT, deleted_at TEXT,
      message_count INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0);
    CREATE TABLE session_messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
      message_id TEXT, model TEXT, tokens_used INTEGER, memories_injected INTEGER,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT);
    CREATE TABLE session_memory_injections (id TEXT PRIMARY KEY, session_id TEXT, memory_id TEXT,
      weight REAL, injected_at TEXT DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT);
    """)
    tx = SimpleNamespace(conn=SimpleNamespace(cursor=lambda: Cursor(conn)))
    repo = repo_type()
    try:
        session = await repo.create_session(tx, user_id="alice", namespace="notes", model="model", initial_context=None)
        sid = session["id"]
        for role, content in [("user", "question"), ("assistant", "answer")]:
            mid = await repo.add_message(tx, session_id=sid, role=role, content=content)
        assert await repo.fetch_provider_history(tx, sid) == [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        rows, total = await repo.fetch_history(tx, sid, 1, 1)
        assert total == 2 and rows[0]["content"] == "answer"
        await repo.add_memory_injections(tx, session_id=sid, message_id=mid, memory_ids=["mem_a", "mem_b"])
        assert set(await repo.list_injected_memory_ids(tx, sid)) == {"mem_a", "mem_b"}
        await repo.update_metrics(tx, session_id=sid, user_id="alice", namespace="notes", tokens_used=5)
        await repo.update_metrics(tx, session_id=sid, user_id="alice", namespace="notes", tokens_used=7)
        session = await repo.get_session(tx, sid, "alice", "notes")
        assert (session["message_count"], session["total_tokens"]) == (4, 12)
        assert not await repo.delete_session(tx, sid, "bob", "notes")
        assert not await repo.delete_session(tx, sid, "alice", "other")
        assert await repo.delete_session(tx, sid, "alice", "notes")
        assert await repo.get_session(tx, sid, "alice", "notes") is None
        assert await repo.fetch_history(tx, sid, 10, 0) == ([], 0)
        assert await repo.list_injected_memory_ids(tx, sid) == []
    finally:
        conn.close()
