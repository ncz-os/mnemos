from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from mnemos.api.dependencies import UserContext
from mnemos.api.routes import audit


@pytest.mark.asyncio
@pytest.mark.parametrize('sealed', [False, True])
async def test_validate_inclusion_memory_visibility(monkeypatch, sealed):
    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace()
    repository = SimpleNamespace(get_audit_entry_by_id=AsyncMock(return_value={
        'memory_id': b'0' * 16, 'global_root': b'1' * 32 if sealed else None}),
        list_window_entries=AsyncMock(return_value=[]))
    memories = SimpleNamespace(get_memory_by_audit_key=AsyncMock(return_value=None))
    backend = SimpleNamespace(transactional=transaction, audit_chain=repository, memories=memories)
    monkeypatch.setattr(audit, '_require_audit_backend', lambda: backend)
    monkeypatch.setattr(audit, 'audit_chain_enabled', lambda: True)
    monkeypatch.setattr(audit, 'maybe_set_pg_rls', AsyncMock())
    user = UserContext(user_id='reader', namespace='default', role='user', group_ids=[], authenticated=True)
    with pytest.raises(HTTPException) as error:
        await audit.audit_inclusion_proof(entry_id='00' * 16, user=user)
    assert error.value.status_code == 404
    repository.list_window_entries.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_visible_audit_key():
    from mnemos.persistence.base import MemoryRepository
    from mnemos.audit.route_helper import memory_id_to_audit_bytes
    visibility = object()
    rows = [{'id': 'sample-memory'}]
    repository = SimpleNamespace(list_memories=AsyncMock(return_value=(rows, 1)))
    result = await MemoryRepository.get_memory_by_audit_key(
        repository, None, memory_id_to_audit_bytes('sample-memory'), visibility=visibility,
        include_archived=True)
    assert result == rows[0]
    assert repository.list_memories.await_args.kwargs['visibility'] is visibility
    repository.list_memories.return_value = ([], 0)
    assert await MemoryRepository.get_memory_by_audit_key(
        repository, None, memory_id_to_audit_bytes('sample-memory'), visibility=visibility) is None


@pytest.mark.asyncio
async def test_return_authorized_inclusion(monkeypatch):
    from mnemos.audit.crypto import merkle_leaf, verify_inclusion
    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace()
    entry = b'0' * 16
    signature = b'1' * 64
    root = merkle_leaf(entry, signature)
    row = {'entry_id': entry, 'signature': signature, 'memory_id': b'2' * 16, 'global_root': root}
    repository = SimpleNamespace(get_audit_entry_by_id=AsyncMock(return_value=row),
                                 list_window_entries=AsyncMock(return_value=[row]))
    backend = SimpleNamespace(transactional=transaction, audit_chain=repository,
                              memories=SimpleNamespace(get_memory_by_audit_key=AsyncMock(return_value={'id': 'sample'})))
    monkeypatch.setattr(audit, '_require_audit_backend', lambda: backend)
    monkeypatch.setattr(audit, 'audit_chain_enabled', lambda: True)
    monkeypatch.setattr(audit, 'maybe_set_pg_rls', AsyncMock())
    user = UserContext(user_id='reader', namespace='default', role='user', group_ids=[], authenticated=True)
    result = await audit.audit_inclusion_proof(entry_id=entry.hex(), user=user)
    assert result['leaf_hash'] == root.hex()
    assert verify_inclusion(root, [], root)
