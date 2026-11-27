"""Validate repository results with an in-memory SQL adapter."""
import sqlite3
from types import SimpleNamespace

import pytest

from mnemos.persistence.oracle import OracleSessionsRepository
from mnemos.persistence.db2 import Db2SessionsRepository


class Cursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def execute(self, sql, params):
        sql = sql.replace('SYSTIMESTAMP', 'CURRENT_TIMESTAMP').replace('TO_CHAR(created_at)', 'created_at')
        sql = sql.replace(' FOR UPDATE', '')
        sql = sql.replace('FETCH FIRST :row_limit ROWS ONLY', 'LIMIT :row_limit')
        sql = sql.replace('OFFSET :row_offset ROWS FETCH NEXT :row_limit ROWS ONLY', 'LIMIT :row_limit OFFSET :row_offset')
        self.cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


@pytest.mark.asyncio
@pytest.mark.parametrize('repository', [OracleSessionsRepository, Db2SessionsRepository])
async def test_session_history_rows_and_scope(repository):
    connection = sqlite3.connect(':memory:')
    connection.executescript('''
        CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, namespace TEXT, model TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_activity TEXT, message_count INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0, deleted_at TEXT);
        CREATE TABLE session_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            role TEXT, content TEXT, model TEXT, tokens_used INTEGER, memories_injected INTEGER,
            message_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT);
        CREATE TABLE session_memory_injections (id TEXT PRIMARY KEY, session_id TEXT, memory_id TEXT,
            weight REAL, injected_at TEXT DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT);
    ''')
    tx = SimpleNamespace(conn=SimpleNamespace(cursor=lambda: Cursor(connection)))
    repo = repository()
    try:
        session = await repo.create_session(tx, user_id='owner', namespace='default', model='example', initial_context='Context')
        sid = session['id']
        mid = await repo.add_message(tx, session_id=sid, role='user', content='First')
        await repo.add_message(tx, session_id=sid, role='assistant', content='Second')
        rows, total = await repo.fetch_history(tx, sid, 2, 1)
        assert total == 3
        assert [row['content'] for row in rows] == ['First', 'Second']
        assert [row['content'] for row in await repo.fetch_provider_history(tx, sid)] == ['Context', 'First', 'Second']
        await repo.add_memory_injections(tx, session_id=sid, message_id=mid, memory_ids=['example'])
        assert await repo.list_injected_memory_ids(tx, sid) == ['example']
        await repo.update_metrics(tx, session_id=sid, user_id='owner', namespace='default', tokens_used=7)
        row = await repo.get_session(tx, sid, 'owner', 'default')
        assert (row['message_count'], row['total_tokens']) == (2, 7)
        assert not await repo.get_session(tx, sid, 'other', 'default')
        assert not await repo.get_session(tx, sid, 'owner', 'other')
        assert not await repo.delete_session(tx, sid, 'other', 'default')
        assert not await repo.delete_session(tx, sid, 'owner', 'other')
        assert await repo.delete_session(tx, sid, 'owner', 'default')
        assert await repo.fetch_history(tx, sid, 10, 0) == ([], 0)
        assert await repo.list_injected_memory_ids(tx, sid) == []
    finally:
        connection.close()
