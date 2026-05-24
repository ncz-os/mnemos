"""Integration tests for v6.2 M-2.2.1 audit sealer worker."""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _audit_root_key(monkeypatch) -> None:
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

    backend = SqliteBackend(tmp_path / "audit.db", _S())
    await backend.open()
    yield backend
    await backend.close()


async def _insert_entry(backend, *, memory_id, prev_id, prev_h, op, ts, writer_id="alice"):
    from mnemos.audit import build_entry, canonical_payload_hash, latest_hash

    ph = canonical_payload_hash(
        memory_id=str(memory_id),
        content=f"content-{op}",
        category="facts",
        subcategory=None,
        metadata={"op": op},
        embedding=None,
    )
    entry, sig = build_entry(
        op=op,
        memory_id=memory_id,
        prev_entry_id=prev_id,
        prev_entry_hash=prev_h,
        payload_hash=ph,
        writer_id=writer_id,
        session_secret=b"x" * 32,
        signed_at=ts,
    )
    async with backend.transactional() as tx:
        await backend.audit_chain.insert_audit_entry(
            tx,
            entry_id=entry.entry_id,
            memory_id=entry.memory_id,
            prev_entry_id=entry.prev_entry_id,
            prev_entry_hash=entry.prev_entry_hash,
            op=entry.op,
            payload_hash=entry.payload_hash,
            writer_id=entry.writer_id,
            writer_pubkey=entry.writer_pubkey,
            signature=sig,
            signed_at=entry.signed_at,
        )
    return entry, sig, latest_hash(entry, sig)


@pytest.mark.asyncio
async def test_seal_three_entry_chain(sqlite_backend):
    from mnemos.workers.audit_sealer import AuditSealer

    mid = uuid.uuid4().bytes
    prev_id = None
    prev_h = None
    for i, op in enumerate(["create", "update", "update"]):
        ts = datetime(2026, 5, 24, 14, i, 0, tzinfo=timezone.utc)
        e, _, prev_h = await _insert_entry(
            sqlite_backend,
            memory_id=mid,
            prev_id=prev_id,
            prev_h=prev_h,
            op=op,
            ts=ts,
        )
        prev_id = e.entry_id

    sealer = AuditSealer(sqlite_backend, window_seconds=1, batch_size=100, poll_interval=1)
    sealed = await sealer.run_once()
    assert sealed == 3

    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.audit_chain.get_latest_audit_entry(tx, mid)
        assert row is not None
        assert row["global_root"] is not None
        assert len(row["global_root"]) == 32
        assert row["global_seq"] is not None


@pytest.mark.asyncio
async def test_second_seal_no_op_after_stamp(sqlite_backend):
    from mnemos.workers.audit_sealer import AuditSealer

    mid = uuid.uuid4().bytes
    await _insert_entry(
        sqlite_backend,
        memory_id=mid,
        prev_id=None,
        prev_h=None,
        op="create",
        ts=datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc),
    )

    sealer = AuditSealer(sqlite_backend, window_seconds=1, batch_size=100, poll_interval=1)
    n1 = await sealer.run_once()
    n2 = await sealer.run_once()
    assert n1 == 1
    assert n2 == 0


@pytest.mark.asyncio
async def test_empty_seal(sqlite_backend):
    from mnemos.workers.audit_sealer import AuditSealer

    sealer = AuditSealer(sqlite_backend, window_seconds=1, batch_size=100, poll_interval=1)
    sealed = await sealer.run_once()
    assert sealed == 0


@pytest.mark.asyncio
async def test_signatures_remain_valid_after_seal(sqlite_backend):
    """Stamping global_root + global_seq must NOT invalidate signatures
    (signature is over canonical bytes that exclude those columns)."""
    from mnemos.audit import AuditEntry, verify_entry
    from mnemos.workers.audit_sealer import AuditSealer

    mid = uuid.uuid4().bytes
    _, sig, _ = await _insert_entry(
        sqlite_backend,
        memory_id=mid,
        prev_id=None,
        prev_h=None,
        op="create",
        ts=datetime(2026, 5, 24, 11, 0, 0, tzinfo=timezone.utc),
    )

    sealer = AuditSealer(sqlite_backend, window_seconds=1, batch_size=100, poll_interval=1)
    await sealer.run_once()

    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.audit_chain.get_latest_audit_entry(tx, mid)
        ent = AuditEntry(
            entry_id=row["entry_id"],
            memory_id=row["memory_id"],
            prev_entry_id=row["prev_entry_id"],
            prev_entry_hash=row["prev_entry_hash"],
            op=row["op"],
            payload_hash=row["payload_hash"],
            writer_id=row["writer_id"],
            writer_pubkey=row["writer_pubkey"],
            signed_at=row["signed_at"],
        )
        assert verify_entry(ent, row["signature"]) is True


@pytest.mark.asyncio
async def test_run_forever_starts_and_stops(sqlite_backend):
    from mnemos.workers.audit_sealer import AuditSealer

    mid = uuid.uuid4().bytes
    await _insert_entry(
        sqlite_backend,
        memory_id=mid,
        prev_id=None,
        prev_h=None,
        op="create",
        ts=datetime(2026, 5, 24, 13, 0, 0, tzinfo=timezone.utc),
    )

    sealer = AuditSealer(sqlite_backend, window_seconds=1, batch_size=100, poll_interval=1)
    task = await sealer.start_background()
    await asyncio.sleep(0.5)  # give it time to seal once
    sealer.stop()
    await asyncio.wait_for(task, timeout=5.0)
    assert task.done()


def test_audit_chain_enabled_helper(monkeypatch):
    from mnemos.workers.audit_sealer import audit_chain_enabled

    monkeypatch.delenv("MNEMOS_AUDIT_CHAIN", raising=False)
    assert audit_chain_enabled() is False
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    assert audit_chain_enabled() is True
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "off")
    assert audit_chain_enabled() is False


def test_init_rejects_backend_without_audit_chain():
    from mnemos.workers.audit_sealer import AuditSealer

    class _Stub:
        audit_chain = None

    with pytest.raises(ValueError, match="no audit_chain"):
        AuditSealer(_Stub())
