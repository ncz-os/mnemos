from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.core.secret_detection import SecretClass, VAULT_NAMESPACE, classify
from mnemos.domain.models import BulkCreateRequest, MemoryCreateRequest, MemorySearchRequest, MemoryUpdateRequest


SECRET_TEXT = "INFRASTRUCTURE CREDENTIALS: TYPHON root login password is ***REMOVED-CREDENTIAL***"
RECORD_TOKEN_TEXT = "INFRASTRUCTURE CREDENTIALS: alice/Tr0ub4dor&3"
RECORD_TOKEN_LITERAL = "Tr0ub4dor&3"


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    from mnemos.persistence import SqliteBackend
    import mnemos.api.routes.memories as memories
    import mnemos.core.lifecycle as lc

    backend = SqliteBackend(tmp_path / "universal_ingest.sqlite3", SimpleNamespace())
    await backend.open()
    monkeypatch.setattr(lc, "_persistence_backend", backend, raising=False)
    monkeypatch.setattr(memories, "_backend_or_503", lambda: backend)
    monkeypatch.setattr(memories, "_get_embedding", lambda _text: _empty_embedding())
    monkeypatch.setattr(memories, "_publish_nats_with_timeout", _noop_async)
    monkeypatch.setattr(memories, "_invalidate_caches_after_mutation", _noop_async)
    monkeypatch.setattr(memories, "_schedule_outbox_deliveries", lambda _ids: None)
    try:
        yield backend
    finally:
        await backend.close()


async def _noop_async(*args, **kwargs):
    return None


def _empty_embedding():
    return None


class _User:
    user_id = "alice"
    namespace = "default"
    role = "user"
    group_ids: list[str] = []
    authenticated = True


class _Root:
    user_id = "root"
    namespace = "default"
    role = "root"
    group_ids: list[str] = []
    authenticated = True


async def _assert_vaulted_and_not_plaintext_searchable(
    backend,
    memory_id: str,
    literal: str = "***REMOVED-CREDENTIAL***",
):
    from mnemos.persistence.visibility import VisibilityFilter

    async with backend.transactional() as tx:
        root_row = await backend.memories.get_memory(
            tx,
            memory_id,
            visibility=VisibilityFilter.for_read(_Root(), namespace=VAULT_NAMESPACE),
        )
        assert root_row is not None
        assert root_row["namespace"] == VAULT_NAMESPACE
        assert literal in root_row["content"]

        default_rows = await backend.memories.fts_search(
            tx,
            query=literal,
            limit=20,
            visibility=VisibilityFilter.for_read(_Root(), namespace=None, include_secrets=False),
        )
        assert all(r["id"] != memory_id for r in default_rows)


@pytest.mark.asyncio
async def test_single_create_vault_refetch_does_not_crash_and_search_hides(sqlite_backend):
    from mnemos.api.routes.memories import create_memory

    item = await create_memory(
        MemoryCreateRequest(content=SECRET_TEXT, category="infrastructure"),
        response=SimpleNamespace(status_code=201),
        user=_User(),
    )
    assert item.id
    assert item.namespace == VAULT_NAMESPACE
    await _assert_vaulted_and_not_plaintext_searchable(sqlite_backend, item.id)


@pytest.mark.asyncio
async def test_credential_record_user_password_pair_vaults_and_search_hides(sqlite_backend):
    from mnemos.api.routes.memories import create_memory

    finding = classify(RECORD_TOKEN_TEXT)
    assert finding.cls is SecretClass.VAULT
    assert finding.spans

    item = await create_memory(
        MemoryCreateRequest(content=RECORD_TOKEN_TEXT, category="infrastructure"),
        response=SimpleNamespace(status_code=201),
        user=_User(),
    )
    assert item.namespace == VAULT_NAMESPACE
    await _assert_vaulted_and_not_plaintext_searchable(
        sqlite_backend,
        item.id,
        RECORD_TOKEN_LITERAL,
    )


@pytest.mark.asyncio
async def test_bulk_create_vaults_and_search_hides(sqlite_backend):
    from mnemos.api.routes.memories import bulk_create_memories

    resp = await bulk_create_memories(
        BulkCreateRequest(memories=[MemoryCreateRequest(content=SECRET_TEXT, category="bulk")]),
        user=_User(),
    )
    assert resp.created == 1
    assert resp.errors == []
    await _assert_vaulted_and_not_plaintext_searchable(sqlite_backend, resp.memory_ids[0])


@pytest.mark.asyncio
async def test_patch_vault_refetch_does_not_rollback(sqlite_backend):
    from mnemos.api.routes.memories import create_memory, update_memory

    created = await create_memory(
        MemoryCreateRequest(content="ordinary operational note", category="notes"),
        response=SimpleNamespace(status_code=201),
        user=_User(),
    )
    patched = await update_memory(created.id, MemoryUpdateRequest(content=SECRET_TEXT), user=_User())
    assert patched.id == created.id
    assert patched.namespace == VAULT_NAMESPACE
    await _assert_vaulted_and_not_plaintext_searchable(sqlite_backend, created.id)


@pytest.mark.asyncio
async def test_route_search_default_does_not_find_vaulted_secret(sqlite_backend):
    from mnemos.api.routes.memories import bulk_create_memories, search_memories

    resp = await bulk_create_memories(
        BulkCreateRequest(memories=[MemoryCreateRequest(content=SECRET_TEXT, category="bulk")]),
        user=_User(),
    )
    search = await search_memories(
        MemorySearchRequest(query="***REMOVED-CREDENTIAL***", semantic=False, limit=20),
        user=_Root(),
    )
    assert resp.memory_ids[0] not in [m.id for m in search.memories]


def test_credential_record_detector_shape():
    finding = classify("🔑 Credential: ssh mini@192.168.207.66 sudo password = 'mini'")
    assert finding.cls is SecretClass.VAULT
    assert finding.spans


def test_federation_nats_event_classifies_content_verbatim_and_compressed():
    from mnemos.federation.nats_consumer import _memory_from_event

    mem = _memory_from_event(
        {
            "content": "normal content",
            "verbatim_content": SECRET_TEXT,
            "compressed_content": "compressed root password ***REMOVED-CREDENTIAL***",
            "namespace": "default",
            "metadata": {},
        },
        peer_name="peer-a",
        memory_id="mem_fed_secret",
    )
    assert mem["namespace"] == VAULT_NAMESPACE
    assert mem["metadata"]["secret_vaulted"] is True


@pytest.mark.asyncio
async def test_document_import_repository_vaults_chunk(sqlite_backend):
    from mnemos.domain.document_repo import DocumentRepository
    from mnemos.persistence.visibility import VisibilityFilter

    repo = DocumentRepository()
    async with sqlite_backend.transactional() as tx:
        imported = await repo.import_chunk(
            sqlite_backend,
            tx,
            memory_id="mem_doc_secret",
            content=SECRET_TEXT,
            category="doc",
            subcategory=None,
            metadata_json="{}",
            owner_id="alice",
            namespace="default",
            permission_mode=600,
            chunk_key="chunk-secret",
            legacy_chunk_key="legacy-secret",
        )
        row = await sqlite_backend.memories.get_memory(
            tx,
            imported.memory_id,
            visibility=VisibilityFilter.for_read(_Root(), namespace=VAULT_NAMESPACE),
        )
    assert row is not None
    assert row["namespace"] == VAULT_NAMESPACE
