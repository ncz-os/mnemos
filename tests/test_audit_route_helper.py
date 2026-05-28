"""Unit tests for v6.2 M-2.2.1 audit-chain route helper."""

from __future__ import annotations

import base64

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


def test_memory_id_to_audit_bytes_deterministic():
    from mnemos.audit.route_helper import memory_id_to_audit_bytes

    mid = "mem_1779637500000_abc123"
    assert memory_id_to_audit_bytes(mid) == memory_id_to_audit_bytes(mid)
    assert len(memory_id_to_audit_bytes(mid)) == 16


def test_memory_id_to_audit_bytes_different_for_different_ids():
    from mnemos.audit.route_helper import memory_id_to_audit_bytes

    assert memory_id_to_audit_bytes("mem_a") != memory_id_to_audit_bytes("mem_b")


def test_memory_id_to_audit_bytes_rejects_empty():
    from mnemos.audit.route_helper import memory_id_to_audit_bytes

    with pytest.raises(ValueError):
        memory_id_to_audit_bytes("")


@pytest.mark.asyncio
async def test_write_audit_entry_create(sqlite_backend):
    from mnemos.audit import write_audit_entry
    from mnemos.audit.route_helper import memory_id_to_audit_bytes

    mid = "mem_1779637500000_abcdef"
    async with sqlite_backend.transactional() as tx:
        await write_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str=mid,
            content="hello world",
            category="facts",
            subcategory=None,
            metadata={"k": "v"},
            embedding=None,
            writer_id="alice",
            session_secret=b"x" * 32,
        )

    # Read back via the chain repo
    mid_bytes = memory_id_to_audit_bytes(mid)
    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.audit_chain.get_latest_audit_entry(tx, mid_bytes)
        assert row is not None
        assert row["op"] == "create"
        assert row["writer_id"] == "alice"
        assert row["prev_entry_id"] is None
        assert row["prev_entry_hash"] is None


@pytest.mark.asyncio
async def test_write_audit_entry_chains_to_prev(sqlite_backend):
    from mnemos.audit import write_audit_entry
    from mnemos.audit.route_helper import memory_id_to_audit_bytes

    mid = "mem_1779637500001_chained"
    async with sqlite_backend.transactional() as tx:
        await write_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str=mid,
            content="v1",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
            writer_id="alice",
            session_secret=b"x" * 32,
        )
    async with sqlite_backend.transactional() as tx:
        await write_audit_entry(
            sqlite_backend,
            tx,
            op="update",
            memory_id_str=mid,
            content="v2",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
            writer_id="alice",
            session_secret=b"x" * 32,
        )

    mid_bytes = memory_id_to_audit_bytes(mid)
    async with sqlite_backend.transactional() as tx:
        # latest is update with prev_entry_id + prev_entry_hash set
        row = await sqlite_backend.audit_chain.get_latest_audit_entry(tx, mid_bytes)
        assert row["op"] == "update"
        assert row["prev_entry_id"] is not None
        assert row["prev_entry_hash"] is not None
        assert len(row["prev_entry_hash"]) == 32


@pytest.mark.asyncio
async def test_write_audit_entry_noop_when_no_audit_chain():
    from mnemos.audit import write_audit_entry

    class _Backend:
        audit_chain = None

    # Should NOT raise; silent no-op
    await write_audit_entry(
        _Backend(),
        None,
        op="create",
        memory_id_str="mem_xx",
        content="hi",
        category="facts",
        subcategory=None,
        metadata=None,
        embedding=None,
        writer_id="alice",
        session_secret=b"x" * 32,
    )


@pytest.mark.asyncio
async def test_write_audit_entry_errors_dont_propagate(sqlite_backend, monkeypatch):
    """If something inside the audit write blows up (e.g. backend hiccup),
    the route handler should NOT see the exception — audit is best-effort."""
    from mnemos.audit import write_audit_entry

    async def _boom(*a, **kw):
        raise RuntimeError("simulated backend failure")

    # Force the repo's insert to blow up
    monkeypatch.setattr(
        sqlite_backend.audit_chain,
        "insert_audit_entry",
        _boom,
    )
    # Must not raise
    async with sqlite_backend.transactional() as tx:
        await write_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str="mem_1779637500002_failboat",
            content="hi",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
            writer_id="alice",
            session_secret=b"x" * 32,
        )
