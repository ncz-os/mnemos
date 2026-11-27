"""Validate session history on configured disposable database services."""
import os
from uuid import uuid4

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('engine,dialect', [('oracle', 'compat'), ('db2', 'compat'), ('db2', 'native')])
async def test_session_history_contract(engine, dialect, monkeypatch):
    dsn = os.getenv(f'MNEMOS_TEST_{engine.upper()}_DSN')
    if not dsn:
        pytest.skip('requires a configured disposable database service')
    from mnemos.core.config import get_settings
    from mnemos.core import lifecycle
    monkeypatch.setenv('MNEMOS_DATABASE_DSN', dsn)
    monkeypatch.setenv('MNEMOS_DB2_DIALECT', dialect)
    _, backend = await lifecycle.build_configured_persistence_backend(get_settings())
    owner = 'session-test-' + uuid4().hex
    session_id = None
    try:
        async with backend.transactional() as tx:
            repo = backend.sessions
            session = await repo.create_session(tx, user_id=owner, namespace='default', model='test', initial_context='System context')
            session_id = session['id']
            message_id = await repo.add_message(tx, session_id=session_id, role='user', content='First turn')
            await repo.add_message(tx, session_id=session_id, role='assistant', content='Second turn')
            await repo.add_memory_injections(tx, session_id=session_id, message_id=message_id, memory_ids=['example-memory'])
            assert await repo.list_injected_memory_ids(tx, session_id) == ['example-memory']
            await repo.update_metrics(tx, session_id=session_id, user_id=owner, namespace='default', tokens_used=7)
            row = await repo.get_session(tx, session_id, owner, 'default')
            assert row['message_count'] == 2
            assert row['total_tokens'] == 7
            history, total = await repo.fetch_history(tx, session_id, 2, 1)
            assert total == 3
            assert [row['content'] for row in history] == ['First turn', 'Second turn']
            assert [row['content'] for row in await repo.fetch_provider_history(tx, session_id)] == ['System context', 'First turn', 'Second turn']
            assert not await repo.get_session(tx, session_id, owner + '-other', 'default')
            assert not await repo.get_session(tx, session_id, owner, 'other')
            assert not await repo.delete_session(tx, session_id, owner + '-other', 'default')
            assert not await repo.delete_session(tx, session_id, owner, 'other')
            assert await repo.delete_session(tx, session_id, owner, 'default')
            assert not await repo.get_session(tx, session_id, owner, 'default')
    finally:
        if session_id:
            async with backend.transactional() as tx:
                await backend.sessions.delete_session(tx, session_id, owner, 'default')
        await backend.close()
