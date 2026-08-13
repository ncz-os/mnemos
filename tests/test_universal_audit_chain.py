from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.domain.models import BulkCreateRequest, MemoryCreateRequest, MemoryUpdateRequest


@pytest.fixture(autouse=True)
def _audit_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    monkeypatch.setenv("MNEMOS_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("MNEMOS_AUDIT_ROOT_PRIVKEY", base64.b64encode(b"\x42" * 32).decode())


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    from mnemos.persistence import SqliteBackend
    import mnemos.api.routes.memories as memories
    import mnemos.core.config as config
    import mnemos.core.lifecycle as lc
    import mnemos.workers.audit_sealer as audit_sealer

    config._settings = None
    backend = SqliteBackend(tmp_path / "universal_audit.sqlite3", SimpleNamespace())
    await backend.open()
    monkeypatch.setattr(lc, "_persistence_backend", backend, raising=False)
    monkeypatch.setattr(memories, "_backend_or_503", lambda: backend)

    async def _empty_embedding(_text):
        return None

    async def _empty_embeddings_batch(texts):
        return [None for _text in texts]

    monkeypatch.setattr(memories, "_get_embedding", _empty_embedding)
    monkeypatch.setattr(memories, "_get_embeddings_batch", _empty_embeddings_batch)
    monkeypatch.setattr(memories, "_publish_nats_with_timeout", _noop_async)
    monkeypatch.setattr(memories, "_invalidate_caches_after_mutation", _noop_async)
    monkeypatch.setattr(memories, "_schedule_outbox_deliveries", lambda _ids: None)
    monkeypatch.setattr(audit_sealer, "audit_chain_enabled", lambda: True)
    try:
        yield backend
    finally:
        await backend.close()


async def _noop_async(*args, **kwargs):
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


class _NoopPgTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePgConn:
    def transaction(self):
        return _NoopPgTransaction()


def _audit_entry_from_row(row):
    from mnemos.audit import AuditEntry

    return AuditEntry(
        entry_id=row["entry_id"],
        memory_id=row["memory_id"],
        prev_entry_id=row.get("prev_entry_id"),
        prev_entry_hash=row.get("prev_entry_hash"),
        op=row["op"],
        payload_hash=row["payload_hash"],
        writer_id=row["writer_id"],
        writer_pubkey=row["writer_pubkey"],
        signed_at=row["signed_at"].isoformat() if hasattr(row["signed_at"], "isoformat") else str(row["signed_at"]),
    )


def _metadata_dict(value):
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    return json.loads(value)


async def _open_source_backend(path):
    from mnemos.persistence import SqliteBackend

    backend = SqliteBackend(path, SimpleNamespace())
    await backend.open()
    return backend


async def _insert_source_memory(source_backend, *, memory_id: str, content: str, updated: str) -> None:
    from mnemos.audit import write_audit_entry

    metadata = {"origin": "source"}
    async with source_backend.transactional() as tx:
        await source_backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content=content,
            category="facts",
            subcategory=None,
            metadata_json=json.dumps(metadata),
            quality_rating=75,
            owner_id="source-owner",
            namespace="default",
            permission_mode=644,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=content,
            embedding=None,
            created=updated,
            updated=updated,
        )
        await write_audit_entry(
            source_backend,
            tx,
            op="create",
            memory_id_str=memory_id,
            content=content,
            category="facts",
            subcategory=None,
            metadata=metadata,
            embedding=None,
            writer_id="source-owner",
            session_secret=b"test-session-secret",
        )


async def _update_source_memory(source_backend, *, memory_id: str, content: str, updated: str) -> None:
    from mnemos.audit import write_audit_entry

    metadata = {"origin": "source"}
    async with source_backend.transactional() as tx:
        await tx.conn.execute(
            "UPDATE memories SET content = ?, verbatim_content = ?, updated = ? WHERE id = ?",
            (content, content, updated, memory_id),
        )
        await write_audit_entry(
            source_backend,
            tx,
            op="update",
            memory_id_str=memory_id,
            content=content,
            category="facts",
            subcategory=None,
            metadata=metadata,
            embedding=None,
            writer_id="source-owner",
            session_secret=b"test-session-secret",
        )


async def _feed_payloads(source_backend, receiver_backend, monkeypatch):
    import mnemos.api.routes.federation as handler
    import mnemos.core.lifecycle as lc

    monkeypatch.setattr(lc, "_persistence_backend", source_backend, raising=False)
    try:
        response = await handler.federation_feed(
            None,
            None,
            since=None,
            namespace=None,
            category=None,
            limit=10,
            prefer_compressed=False,
            copy_embeddings=False,
        )
    finally:
        monkeypatch.setattr(lc, "_persistence_backend", receiver_backend, raising=False)
    return [item.model_dump(mode="json", exclude_none=True) for item in response.memories]


@pytest.mark.asyncio
async def test_bulk_create_writes_verifiable_audit_chain_entries(sqlite_backend):
    from mnemos.api.routes.memories import bulk_create_memories
    from mnemos.audit import AuditEntry, memory_id_to_audit_bytes, verify_entry

    resp = await bulk_create_memories(
        BulkCreateRequest(
            memories=[
                MemoryCreateRequest(content="bulk audit one", category="facts"),
                MemoryCreateRequest(content="bulk audit two", category="facts"),
            ]
        ),
        user=_User(),
    )

    assert resp.created == 2
    assert resp.errors == []
    async with sqlite_backend.transactional() as tx:
        for memory_id in resp.memory_ids:
            row = await sqlite_backend.audit_chain.get_latest_audit_entry(
                tx,
                memory_id_to_audit_bytes(memory_id),
            )
            assert row is not None
            assert row["op"] == "create"
            entry = AuditEntry(
                entry_id=row["entry_id"],
                memory_id=row["memory_id"],
                prev_entry_id=row.get("prev_entry_id"),
                prev_entry_hash=row.get("prev_entry_hash"),
                op=row["op"],
                payload_hash=row["payload_hash"],
                writer_id=row["writer_id"],
                writer_pubkey=row["writer_pubkey"],
                signed_at=row["signed_at"].isoformat()
                if hasattr(row["signed_at"], "isoformat")
                else str(row["signed_at"]),
            )
            assert verify_entry(entry, row["signature"])


@pytest.mark.asyncio
async def test_federation_real_feed_source_head_seeds_and_extends_replica_chain(sqlite_backend, tmp_path, monkeypatch):
    from mnemos.audit import AuditChainContinuityError, AuditEntry, entry_hash, memory_id_to_audit_bytes, verify_entry
    from mnemos.domain.federation import _store_memories
    from mnemos.persistence.visibility import VisibilityFilter

    peer_name = "peer-a"
    remote_id = "mem_remote_1"
    local_id = f"fed:{peer_name}:{remote_id}"

    source_backend = await _open_source_backend(tmp_path / "source.sqlite3")
    try:
        await _insert_source_memory(
            source_backend,
            memory_id=remote_id,
            content="remote v1",
            updated="2026-06-14T20:00:00+00:00",
        )
        first_payload = (await _feed_payloads(source_backend, sqlite_backend, monkeypatch))[0]

        assert first_payload["id"] == remote_id
        assert first_payload["audit_latest_entry_id"]
        assert first_payload["audit_latest_entry_hash"]

        async with sqlite_backend.transactional() as tx:
            new_n, upd_n = await _store_memories(
                sqlite_backend.federation,
                tx,
                peer_name,
                [first_payload],
                backend=sqlite_backend,
            )
        assert (new_n, upd_n) == (1, 0)

        async with sqlite_backend.transactional() as tx:
            first = await sqlite_backend.audit_chain.get_latest_audit_entry(
                tx,
                memory_id_to_audit_bytes(local_id),
            )
            replica = await sqlite_backend.memories.get_memory(
                tx,
                local_id,
                visibility=VisibilityFilter.for_read(_Root(), namespace="default"),
            )
        assert first is not None
        assert replica is not None
        replica_metadata = _metadata_dict(replica["metadata"])
        assert replica_metadata["federation_source_audit_latest_entry_id"] == first_payload["audit_latest_entry_id"]
        assert replica_metadata["federation_source_audit_latest_entry_hash"] == first_payload["audit_latest_entry_hash"]

        first_entry = AuditEntry(
            entry_id=first["entry_id"],
            memory_id=first["memory_id"],
            prev_entry_id=first.get("prev_entry_id"),
            prev_entry_hash=first.get("prev_entry_hash"),
            op=first["op"],
            payload_hash=first["payload_hash"],
            writer_id=first["writer_id"],
            writer_pubkey=first["writer_pubkey"],
            signed_at=first["signed_at"].isoformat()
            if hasattr(first["signed_at"], "isoformat")
            else str(first["signed_at"]),
        )
        assert first["op"] == "replicate"
        assert first["prev_entry_id"] is None
        assert first["prev_entry_hash"] is None
        assert verify_entry(first_entry, first["signature"])

        await _update_source_memory(
            source_backend,
            memory_id=remote_id,
            content="remote v2",
            updated="2026-06-14T20:05:00+00:00",
        )
        second_payload = (await _feed_payloads(source_backend, sqlite_backend, monkeypatch))[0]
        assert second_payload["audit_latest_entry_id"] != first["entry_id"].hex()

        async with sqlite_backend.transactional() as tx:
            new_n, upd_n = await _store_memories(
                sqlite_backend.federation,
                tx,
                peer_name,
                [second_payload],
                backend=sqlite_backend,
            )
        assert (new_n, upd_n) == (0, 1)

        async with sqlite_backend.transactional() as tx:
            second = await sqlite_backend.audit_chain.get_latest_audit_entry(
                tx,
                memory_id_to_audit_bytes(local_id),
            )
        assert second is not None
        assert second["entry_id"] != first["entry_id"]
        assert second["prev_entry_id"] == first["entry_id"]
        assert second["prev_entry_hash"] == entry_hash(first_entry, first["signature"])
        second_entry = AuditEntry(
            entry_id=second["entry_id"],
            memory_id=second["memory_id"],
            prev_entry_id=second.get("prev_entry_id"),
            prev_entry_hash=second.get("prev_entry_hash"),
            op=second["op"],
            payload_hash=second["payload_hash"],
            writer_id=second["writer_id"],
            writer_pubkey=second["writer_pubkey"],
            signed_at=second["signed_at"].isoformat()
            if hasattr(second["signed_at"], "isoformat")
            else str(second["signed_at"]),
        )
        assert verify_entry(second_entry, second["signature"])

        async with sqlite_backend.transactional() as tx:
            await tx.conn.execute(
                "UPDATE memory_audit_chain SET signature = ? WHERE entry_id = ?",
                (b"\x00" * 64, second["entry_id"]),
            )

        await _update_source_memory(
            source_backend,
            memory_id=remote_id,
            content="remote v3",
            updated="2026-06-14T20:10:00+00:00",
        )
        third_payload = (await _feed_payloads(source_backend, sqlite_backend, monkeypatch))[0]

        with pytest.raises(AuditChainContinuityError):
            async with sqlite_backend.transactional() as tx:
                await _store_memories(
                    sqlite_backend.federation,
                    tx,
                    peer_name,
                    [third_payload],
                    backend=sqlite_backend,
                )
    finally:
        await source_backend.close()


@pytest.mark.asyncio
async def test_document_import_writes_create_audit_entry(sqlite_backend):
    from mnemos.audit import canonical_payload_hash, memory_id_to_audit_bytes, verify_entry
    from mnemos.domain.document_repo import DocumentRepository

    repo = DocumentRepository()
    async with sqlite_backend.transactional() as tx:
        imported = await repo.import_chunk(
            sqlite_backend,
            tx,
            memory_id="mem_doc_audit",
            content="document import audit chunk",
            category="documents",
            subcategory="audit",
            metadata_json=json.dumps({"project_tag": "mnemos"}),
            owner_id="alice",
            namespace="default",
            permission_mode=600,
            chunk_key="doc-audit-chunk",
            legacy_chunk_key="doc-audit-legacy",
        )
        row = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx,
            memory_id_to_audit_bytes(imported.memory_id),
        )

    assert row is not None
    assert row["op"] == "create"
    assert row["writer_id"] == "alice"
    assert row["payload_hash"] == canonical_payload_hash(
        memory_id=imported.memory_id,
        content="document import audit chunk",
        category="documents",
        subcategory="audit",
        metadata={"project_tag": "mnemos"},
        embedding=None,
    )
    assert verify_entry(_audit_entry_from_row(row), row["signature"])


@pytest.mark.asyncio
async def test_mpf_import_writes_create_audit_entry(sqlite_backend, monkeypatch):
    # mnemos.domain.portability ships in the optional CHARON distribution.
    pytest.importorskip("mnemos.domain.portability")
    import mnemos.domain.portability.import_ as import_mod
    from mnemos.audit import canonical_payload_hash, memory_id_to_audit_bytes, verify_entry
    from mnemos.domain.portability.schemas import MEMORY_PAYLOAD_VERSION, MPFEnvelope, MPFRecord

    async def _sqlite_insert_memory(_conn, **kwargs):
        await sqlite_backend.memories.insert_memory(
            active_tx,
            memory_id=kwargs["memory_id"],
            content=kwargs["content"],
            category=kwargs["category"],
            subcategory=kwargs["subcategory"],
            metadata_json=kwargs["metadata_json"],
            quality_rating=kwargs["quality_rating"],
            owner_id=kwargs["owner_id"],
            namespace=kwargs["namespace"],
            permission_mode=kwargs["permission_mode"],
            source_model=kwargs["source_model"],
            source_provider=kwargs["source_provider"],
            source_session=kwargs["source_session"],
            source_agent=kwargs["source_agent"],
            verbatim_content=kwargs["verbatim_content"],
            embedding=None,
            created=kwargs["created"],
            updated=kwargs["updated"],
        )
        return "INSERT 0 1"

    monkeypatch.setattr(import_mod.repo, "insert_memory", _sqlite_insert_memory)
    envelope = MPFEnvelope(
        records=[
            MPFRecord(
                id="mem_mpf_audit",
                kind="memory",
                payload_version=MEMORY_PAYLOAD_VERSION,
                payload={
                    "content": "mpf imported audited memory",
                    "category": "portable",
                    "subcategory": None,
                    "metadata": {"imported": True},
                    "owner_id": "alice",
                    "namespace": "default",
                    "permission_mode": 600,
                },
            )
        ]
    )

    async with sqlite_backend.transactional() as tx:
        active_tx = tx
        stats = await import_mod.import_memories(
            _FakePgConn(),
            envelope=envelope,
            preserve_owner=True,
            user=_Root(),
            backend=sqlite_backend,
            tx=tx,
        )
        row = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx,
            memory_id_to_audit_bytes("mem_mpf_audit"),
        )

    assert stats.imported == 1
    assert stats.failed == 0
    assert row is not None
    assert row["op"] == "create"
    assert row["writer_id"] == "root"
    assert row["payload_hash"] == canonical_payload_hash(
        memory_id="mem_mpf_audit",
        content="mpf imported audited memory",
        category="portable",
        subcategory=None,
        metadata={"imported": True},
        embedding=None,
    )
    assert verify_entry(_audit_entry_from_row(row), row["signature"])


@pytest.mark.asyncio
async def test_no_replicate_audit_entry_on_stale_noop(sqlite_backend):
    from mnemos.audit import memory_id_to_audit_bytes
    from mnemos.domain.federation import _store_memories

    peer_name = "peer-stale"
    remote_id = "mem_remote_stale"
    local_id = f"fed:{peer_name}:{remote_id}"
    first_payload = {
        "id": remote_id,
        "content": "remote current",
        "category": "facts",
        "subcategory": None,
        "metadata": {},
        "verbatim_content": "remote current",
        "quality_rating": 75,
        "namespace": "default",
        "updated": "2026-06-14T22:00:00+00:00",
    }
    async with sqlite_backend.transactional() as tx:
        assert await _store_memories(
            sqlite_backend.federation, tx, peer_name, [first_payload], backend=sqlite_backend
        ) == (
            1,
            0,
        )
        first = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx,
            memory_id_to_audit_bytes(local_id),
        )

    stale_payload = {
        **first_payload,
        "content": "remote stale",
        "verbatim_content": "remote stale",
        "updated": "2026-06-14T21:59:00+00:00",
    }
    async with sqlite_backend.transactional() as tx:
        assert await _store_memories(
            sqlite_backend.federation, tx, peer_name, [stale_payload], backend=sqlite_backend
        ) == (
            0,
            0,
        )
        latest = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx,
            memory_id_to_audit_bytes(local_id),
        )

    assert latest is not None
    assert first is not None
    assert latest["entry_id"] == first["entry_id"]


@pytest.mark.asyncio
async def test_audit_payload_hash_uses_persisted_vault_metadata_on_create_and_update(sqlite_backend):
    from mnemos.api.routes.memories import create_memory, update_memory
    from mnemos.audit import canonical_payload_hash, memory_id_to_audit_bytes
    from mnemos.core.secret_detection import VAULT_NAMESPACE
    from mnemos.persistence.visibility import VisibilityFilter

    secret_text = "INFRASTRUCTURE CREDENTIALS: TYPHON root login password is DenylistSelfTest2NotReal99"
    created = await create_memory(
        MemoryCreateRequest(content=secret_text, category="infrastructure"),
        response=SimpleNamespace(status_code=201),
        user=_User(),
    )
    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.memories.get_memory(
            tx,
            created.id,
            visibility=VisibilityFilter.for_read(_Root(), namespace=VAULT_NAMESPACE),
        )
        audit = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx,
            memory_id_to_audit_bytes(created.id),
        )
    assert row is not None
    assert audit is not None
    create_metadata = _metadata_dict(row["metadata"])
    assert row["namespace"] == VAULT_NAMESPACE
    assert create_metadata["secret_vaulted"] is True
    assert audit["payload_hash"] == canonical_payload_hash(
        memory_id=created.id,
        content=secret_text,
        category="infrastructure",
        subcategory=None,
        metadata=create_metadata,
        embedding=None,
    )

    ordinary = await create_memory(
        MemoryCreateRequest(content="ordinary operational note", category="notes"),
        response=SimpleNamespace(status_code=201),
        user=_User(),
    )
    patched = await update_memory(ordinary.id, MemoryUpdateRequest(content=secret_text), user=_User())
    async with sqlite_backend.transactional() as tx:
        patched_row = await sqlite_backend.memories.get_memory(
            tx,
            patched.id,
            visibility=VisibilityFilter.for_read(_Root(), namespace=VAULT_NAMESPACE),
        )
        patched_audit = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx,
            memory_id_to_audit_bytes(patched.id),
        )
    assert patched_row is not None
    assert patched_audit is not None
    update_metadata = _metadata_dict(patched_row["metadata"])
    assert patched_row["namespace"] == VAULT_NAMESPACE
    assert update_metadata["secret_vaulted"] is True
    assert patched_audit["op"] == "update"
    assert patched_audit["payload_hash"] == canonical_payload_hash(
        memory_id=patched.id,
        content=secret_text,
        category="notes",
        subcategory=None,
        metadata=update_metadata,
        embedding=None,
    )
