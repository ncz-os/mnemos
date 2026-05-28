"""Integration tests for v6.2 M-2.2.1 audit repository methods that
landed in the cleanup pass (commits a9069b9 + aa8b96f):

* `get_audit_entry_by_id(tx, entry_id) -> Row | None`
* `get_latest_audit_entries_batch(tx, memory_ids) -> dict[bytes, Row]`
* `list_window_entries(tx, global_root) -> list[Row]`

All tested under SQLite backend (single-writer + portable enough
for CI); PG / Oracle / Db2 share the contract via base.py abstract.
"""

from __future__ import annotations

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


async def _insert(backend, memory_id: bytes, ts: datetime, op: str = "create"):
    from mnemos.audit import build_entry, canonical_payload_hash

    ph = canonical_payload_hash(
        memory_id=str(memory_id),
        content=f"c-{op}",
        category="facts",
        subcategory=None,
        metadata=None,
        embedding=None,
    )
    entry, sig = build_entry(
        op=op,
        memory_id=memory_id,
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
    return entry, sig


@pytest.mark.asyncio
async def test_get_audit_entry_by_id(sqlite_backend):
    mid = uuid.uuid4().bytes
    entry, _ = await _insert(
        sqlite_backend,
        mid,
        datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc),
    )

    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.audit_chain.get_audit_entry_by_id(tx, entry.entry_id)
        assert row is not None
        assert row["entry_id"] == entry.entry_id
        assert row["memory_id"] == mid
        assert row["op"] == "create"
        assert row["writer_id"] == "alice"
        assert row["global_root"] is None  # unsealed


@pytest.mark.asyncio
async def test_get_audit_entry_by_id_404_for_unknown(sqlite_backend):
    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.audit_chain.get_audit_entry_by_id(tx, b"\xde" * 16)
        assert row is None


@pytest.mark.asyncio
async def test_batch_empty_list_returns_empty_dict(sqlite_backend):
    async with sqlite_backend.transactional() as tx:
        out = await sqlite_backend.audit_chain.get_latest_audit_entries_batch(tx, [])
        assert out == {}


@pytest.mark.asyncio
async def test_batch_returns_latest_per_memory(sqlite_backend):
    """Insert 2 entries for one memory_id + 1 for another. Batch
    returns latest per memory_id."""
    mid_a = uuid.uuid4().bytes
    mid_b = uuid.uuid4().bytes
    # Two writes for mid_a; the second is newer
    await _insert(sqlite_backend, mid_a, datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc))
    entry_a2, _ = await _insert(
        sqlite_backend,
        mid_a,
        datetime(2026, 5, 24, 10, 0, 0, tzinfo=timezone.utc),
        op="update",
    )
    entry_b, _ = await _insert(
        sqlite_backend,
        mid_b,
        datetime(2026, 5, 24, 9, 30, 0, tzinfo=timezone.utc),
    )

    async with sqlite_backend.transactional() as tx:
        out = await sqlite_backend.audit_chain.get_latest_audit_entries_batch(tx, [mid_a, mid_b])
        assert set(out.keys()) == {mid_a, mid_b}
        # mid_a latest is the newer "update" entry, NOT the original "create"
        assert out[mid_a]["op"] == "update"
        assert out[mid_a]["entry_id"] == entry_a2.entry_id
        assert out[mid_b]["op"] == "create"
        assert out[mid_b]["entry_id"] == entry_b.entry_id


@pytest.mark.asyncio
async def test_batch_omits_unknown_memory_ids(sqlite_backend):
    mid_real = uuid.uuid4().bytes
    mid_fake = b"\xff" * 16
    await _insert(
        sqlite_backend,
        mid_real,
        datetime(2026, 5, 24, 8, 0, 0, tzinfo=timezone.utc),
    )
    async with sqlite_backend.transactional() as tx:
        out = await sqlite_backend.audit_chain.get_latest_audit_entries_batch(tx, [mid_real, mid_fake])
        assert mid_real in out
        assert mid_fake not in out


@pytest.mark.asyncio
async def test_list_window_entries_returns_sorted(sqlite_backend):
    """After sealer stamps a window, list_window_entries returns rows
    sorted by signed_at, entry_id -- same order sealer used."""
    from mnemos.workers.audit_sealer import AuditSealer

    for i in range(4):
        await _insert(
            sqlite_backend,
            uuid.uuid4().bytes,
            datetime(2026, 5, 24, 7, i, 0, tzinfo=timezone.utc),
        )
    sealer = AuditSealer(sqlite_backend, window_seconds=1, batch_size=100, poll_interval=1)
    await sealer.run_once()

    async with sqlite_backend.transactional() as tx:
        # Fetch one stamped row to get the root
        from mnemos.persistence.sqlite import _fetch_one

        sample = await _fetch_one(
            sqlite_backend._conn,
            "SELECT global_root FROM memory_audit_chain LIMIT 1",
            (),
        )
        assert sample["global_root"] is not None
        rows = await sqlite_backend.audit_chain.list_window_entries(tx, sample["global_root"])
        assert len(rows) == 4
        # Strict ordering check
        signed_ats = [r["signed_at"] for r in rows]
        assert signed_ats == sorted(signed_ats)


@pytest.mark.asyncio
async def test_list_window_entries_unknown_root(sqlite_backend):
    async with sqlite_backend.transactional() as tx:
        rows = await sqlite_backend.audit_chain.list_window_entries(tx, b"\xee" * 32)
        assert rows == []
