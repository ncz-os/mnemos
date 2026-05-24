"""Integration tests for v6.2 M-2.2.1 /v1/audit/health endpoint + repo stats."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _audit_env(monkeypatch):
    monkeypatch.setenv(
        "MNEMOS_AUDIT_ROOT_PRIVKEY",
        base64.b64encode(b"\x42" * 32).decode(),
    )


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    class _S:
        class database:
            embedding_dim = 1024

    backend = SqliteBackend(tmp_path / "health.db", _S())
    await backend.open()
    yield backend
    await backend.close()


async def _insert_one(backend, ts: datetime):
    from mnemos.audit import build_entry, canonical_payload_hash

    mid = uuid.uuid4().bytes
    ph = canonical_payload_hash(
        memory_id=str(mid),
        content="hi",
        category="facts",
        subcategory=None,
        metadata=None,
        embedding=None,
    )
    entry, sig = build_entry(
        op="create",
        memory_id=mid,
        prev_entry_id=None,
        prev_entry_hash=None,
        payload_hash=ph,
        writer_id="alice",
        session_secret=b"x" * 32,
        signed_at=ts,
    )
    async with backend.transactional() as tx:
        await backend.audit_chain.insert_audit_entry(
            tx,
            entry_id=entry.entry_id,
            memory_id=entry.memory_id,
            prev_entry_id=None,
            prev_entry_hash=None,
            op=entry.op,
            payload_hash=entry.payload_hash,
            writer_id=entry.writer_id,
            writer_pubkey=entry.writer_pubkey,
            signature=sig,
            signed_at=entry.signed_at,
        )


@pytest.mark.asyncio
async def test_chain_stats_empty(sqlite_backend):
    async with sqlite_backend.transactional() as tx:
        stats = await sqlite_backend.audit_chain.get_chain_stats(tx)
    assert stats["total_entries"] == 0
    assert stats["unsealed_count"] == 0
    assert stats["oldest_unsealed_signed_at"] is None
    assert stats["sealed_root_count"] == 0
    assert stats["last_sealed_at"] is None


@pytest.mark.asyncio
async def test_chain_stats_after_inserts(sqlite_backend):
    for i in range(3):
        await _insert_one(
            sqlite_backend,
            datetime(2026, 5, 24, 10, i, 0, tzinfo=timezone.utc),
        )
    async with sqlite_backend.transactional() as tx:
        stats = await sqlite_backend.audit_chain.get_chain_stats(tx)
    assert stats["total_entries"] == 3
    assert stats["unsealed_count"] == 3
    assert stats["oldest_unsealed_signed_at"] == "2026-05-24T10:00:00+00:00"
    assert stats["sealed_root_count"] == 0


@pytest.mark.asyncio
async def test_chain_stats_after_seal(sqlite_backend):
    from mnemos.workers.audit_sealer import AuditSealer

    for i in range(2):
        await _insert_one(
            sqlite_backend,
            datetime(2026, 5, 24, 9, i, 0, tzinfo=timezone.utc),
        )
    sealer = AuditSealer(sqlite_backend, window_seconds=1, batch_size=100, poll_interval=1)
    await sealer.run_once()

    async with sqlite_backend.transactional() as tx:
        stats = await sqlite_backend.audit_chain.get_chain_stats(tx)
    assert stats["total_entries"] == 2
    assert stats["unsealed_count"] == 0
    assert stats["oldest_unsealed_signed_at"] is None
    assert stats["sealed_root_count"] == 1
    assert stats["last_sealed_at"] is not None


@pytest.fixture
def fake_user():
    from mnemos.api.dependencies import UserContext

    return UserContext(
        user_id="user-1",
        group_ids=[],
        role="user",
        namespace="default",
        authenticated=True,
    )


@pytest.mark.asyncio
async def test_health_endpoint_returns_stats(sqlite_backend, fake_user, monkeypatch):
    from mnemos.api.routes import audit as audit_route

    monkeypatch.setattr(audit_route, "audit_chain_enabled", lambda: True)
    monkeypatch.setattr(
        "mnemos.api.routes.audit._backend_or_503",
        lambda: sqlite_backend,
    )

    # Insert 2 + seal 1 by hand to populate stats
    await _insert_one(
        sqlite_backend,
        datetime(2026, 5, 24, 8, 0, 0, tzinfo=timezone.utc),
    )
    await _insert_one(
        sqlite_backend,
        datetime(2026, 5, 24, 8, 30, 0, tzinfo=timezone.utc),
    )

    out = await audit_route.audit_health(user=fake_user)
    assert out["chain_enabled"] is True
    assert out["backend_has_audit_chain"] is True
    assert out["total_entries"] == 2
    assert out["unsealed_count"] == 2
    assert "oldest_unsealed_age_seconds" in out
    assert out["oldest_unsealed_age_seconds"] >= 0


@pytest.mark.asyncio
async def test_health_endpoint_when_chain_disabled(sqlite_backend, fake_user, monkeypatch):
    from mnemos.api.routes import audit as audit_route

    monkeypatch.setattr(audit_route, "audit_chain_enabled", lambda: False)
    monkeypatch.setattr(
        "mnemos.api.routes.audit._backend_or_503",
        lambda: sqlite_backend,
    )

    out = await audit_route.audit_health(user=fake_user)
    # Still returns stats; just reports chain_enabled=False
    assert out["chain_enabled"] is False
    assert out["backend_has_audit_chain"] is True
    assert "total_entries" in out
