from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.domain.models import BulkCreateRequest, MemoryCreateRequest


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

    monkeypatch.setattr(memories, "_get_embedding", _empty_embedding)
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
                signed_at=row["signed_at"].isoformat() if hasattr(row["signed_at"], "isoformat") else str(row["signed_at"]),
            )
            assert verify_entry(entry, row["signature"])


@pytest.mark.asyncio
async def test_federation_inbound_writes_verifiable_chain_and_enforces_head(sqlite_backend):
    from mnemos.audit import AuditChainContinuityError, AuditEntry, entry_hash, memory_id_to_audit_bytes, verify_entry
    from mnemos.domain.federation import _store_memories

    peer_name = "peer-a"
    remote_id = "mem_remote_1"
    local_id = f"fed:{peer_name}:{remote_id}"
    first_payload = {
        "id": remote_id,
        "content": "remote v1",
        "category": "facts",
        "subcategory": None,
        "metadata": {"k": "v"},
        "verbatim_content": "remote v1",
        "quality_rating": 75,
        "namespace": "default",
        "updated": "2026-06-14T20:00:00+00:00",
    }

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
    assert first is not None
    first_entry = AuditEntry(
        entry_id=first["entry_id"],
        memory_id=first["memory_id"],
        prev_entry_id=first.get("prev_entry_id"),
        prev_entry_hash=first.get("prev_entry_hash"),
        op=first["op"],
        payload_hash=first["payload_hash"],
        writer_id=first["writer_id"],
        writer_pubkey=first["writer_pubkey"],
        signed_at=first["signed_at"].isoformat() if hasattr(first["signed_at"], "isoformat") else str(first["signed_at"]),
    )
    assert first["op"] == "replicate"
    assert verify_entry(first_entry, first["signature"])

    second_payload = {
        **first_payload,
        "content": "remote v2",
        "verbatim_content": "remote v2",
        "updated": "2026-06-14T20:05:00+00:00",
        "audit_latest_entry_id": first["entry_id"].hex(),
        "audit_latest_entry_hash": entry_hash(first_entry, first["signature"]).hex(),
    }
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
        signed_at=second["signed_at"].isoformat() if hasattr(second["signed_at"], "isoformat") else str(second["signed_at"]),
    )
    assert verify_entry(second_entry, second["signature"])

    bad_payload = {
        **first_payload,
        "content": "remote v3",
        "verbatim_content": "remote v3",
        "updated": "2026-06-14T20:10:00+00:00",
        "audit_latest_entry_id": first["entry_id"].hex(),
        "audit_latest_entry_hash": "00" * 32,
    }
    with pytest.raises(AuditChainContinuityError):
        async with sqlite_backend.transactional() as tx:
            await _store_memories(
                sqlite_backend.federation,
                tx,
                peer_name,
                [bad_payload],
                backend=sqlite_backend,
            )
