"""Regression tests for universal memory mutation audit-chain coverage."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _audit_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    monkeypatch.setenv("MNEMOS_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("MNEMOS_AUDIT_ROOT_PRIVKEY", base64.b64encode(b"\x42" * 32).decode())
    import mnemos.core.config as config

    config._settings = None
    yield
    config._settings = None


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    from mnemos.persistence import SqliteBackend
    import mnemos.api.routes.memories as memories
    import mnemos.core.lifecycle as lc

    backend = SqliteBackend(tmp_path / "universal_audit.sqlite3", SimpleNamespace())
    await backend.open()
    monkeypatch.setattr(lc, "_persistence_backend", backend, raising=False)
    monkeypatch.setattr(memories, "_backend_or_503", lambda: backend)
    monkeypatch.setattr(memories, "_get_embedding", lambda _text: None)
    monkeypatch.setattr(memories, "_publish_nats_with_timeout", _noop_async)
    monkeypatch.setattr(memories, "_invalidate_caches_after_mutation", _noop_async)
    monkeypatch.setattr(memories, "_schedule_outbox_deliveries", lambda _ids: None)
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
    authenticated = True
    group_ids = []


@pytest.mark.asyncio
async def test_bulk_create_writes_verifiable_audit_chain_entries(sqlite_backend):
    from mnemos.api.routes.memories import bulk_create_memories
    from mnemos.audit import memory_id_to_audit_bytes, verify_entry
    from mnemos.audit.crypto import AuditEntry
    from mnemos.domain.models import BulkCreateRequest, MemoryCreateRequest

    resp = await bulk_create_memories(
        BulkCreateRequest(
            memories=[
                MemoryCreateRequest(content="bulk audit one", category="facts"),
                MemoryCreateRequest(content="bulk audit two", category="notes"),
            ]
        ),
        user=_User(),
    )

    assert resp.errors == []
    assert resp.created == 2
    assert len(resp.memory_ids) == 2

    async with sqlite_backend.transactional() as tx:
        for memory_id in resp.memory_ids:
            row = await sqlite_backend.audit_chain.get_latest_audit_entry(
                tx,
                memory_id_to_audit_bytes(memory_id),
            )
            assert row is not None
            assert row["op"] == "create"
            assert row["writer_id"] == "alice"
            entry = AuditEntry(
                entry_id=row["entry_id"],
                memory_id=row["memory_id"],
                prev_entry_id=row.get("prev_entry_id"),
                prev_entry_hash=row.get("prev_entry_hash"),
                op=row["op"],
                payload_hash=row["payload_hash"],
                writer_id=row["writer_id"],
                writer_pubkey=row["writer_pubkey"],
                signed_at=row["signed_at"],
            )
            assert verify_entry(entry, row["signature"])


@pytest.mark.asyncio
async def test_federation_inbound_writes_replicate_audit_entry(sqlite_backend):
    from mnemos.audit import memory_id_to_audit_bytes, verify_entry
    from mnemos.audit.crypto import AuditEntry
    from mnemos.domain.federation import _store_memories

    feed = [
        {
            "id": "remote-audit-1",
            "content": "federated audited payload",
            "category": "facts",
            "subcategory": None,
            "verbatim_content": "federated audited payload",
            "namespace": "default",
            "metadata": {"source": "test"},
            "quality_rating": 75,
            "updated": "2026-06-14T18:00:00Z",
            "created": "2026-06-14T18:00:00Z",
        }
    ]

    async with sqlite_backend.transactional() as tx:
        new_n, upd_n = await _store_memories(sqlite_backend.federation, tx, "pythia", feed, backend=sqlite_backend)

    assert (new_n, upd_n) == (1, 0)
    local_id = "fed:pythia:remote-audit-1"
    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.audit_chain.get_latest_audit_entry(tx, memory_id_to_audit_bytes(local_id))
        assert row is not None
        assert row["op"] == "replicate"
        assert row["writer_id"] == "fed:pythia"
        entry = AuditEntry(
            entry_id=row["entry_id"],
            memory_id=row["memory_id"],
            prev_entry_id=row.get("prev_entry_id"),
            prev_entry_hash=row.get("prev_entry_hash"),
            op=row["op"],
            payload_hash=row["payload_hash"],
            writer_id=row["writer_id"],
            writer_pubkey=row["writer_pubkey"],
            signed_at=row["signed_at"],
        )
        assert verify_entry(entry, row["signature"])
