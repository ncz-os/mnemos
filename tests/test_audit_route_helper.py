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


# ─── F16: required audit writes MUST propagate, embedding=None fix ──────────
#
# F16 split the audit bridge into two modes:
#   * default (best-effort): errors logged + swallowed, no exception
#     bubbles. Backward-compatible with every existing caller.
#   * required=True: errors propagate as AuditChainContinuityError so the
#     surrounding ``async with backend.transactional()`` rolls back the
#     mutation. This is the documented contract for archive + delete
#     paths where audit coverage is REQUIRED (every mutation must hit
#     the chain or the entire write must abort).


@pytest.mark.asyncio
async def test_write_audit_entry_required_propagates_insert_failure(sqlite_backend, monkeypatch):
    """F16: a REQUIRED audit write must NOT silently swallow insert
    failures — the route handler sits inside ``async with tx:`` and the
    propagation rolls the mutation back so the row never commits
    without its audit trail."""
    from mnemos.audit import write_audit_entry
    from mnemos.audit.route_helper import AuditChainContinuityError

    async def _boom(*a, **kw):
        raise RuntimeError("simulated backend hiccup")

    monkeypatch.setattr(
        sqlite_backend.audit_chain,
        "insert_audit_entry",
        _boom,
    )

    raised = None
    async with sqlite_backend.transactional() as tx:
        try:
            await write_audit_entry(
                sqlite_backend,
                tx,
                op="archive",
                memory_id_str="mem_required_fail",
                content="important content",
                category="facts",
                subcategory=None,
                metadata=None,
                embedding=None,
                writer_id="alice",
                session_secret=b"x" * 32,
                required=True,
            )
        except AuditChainContinuityError as exc:
            raised = exc

    assert raised is not None, (
        "required=True audit write MUST propagate insert failures as "
        "AuditChainContinuityError so the surrounding transaction rolls back"
    )
    assert "required audit write failed" in str(raised)


@pytest.mark.asyncio
async def test_write_audit_entry_required_raises_when_no_chain_repo():
    """F16: a REQUIRED audit write against a backend with no audit_chain
    repo (e.g., MySQL/MariaDB) must raise — not silently no-op. The
    caller wants a guarantee; the absence of a chain repo is a hard
    refusal, not a degraded success."""
    from mnemos.audit import write_audit_entry
    from mnemos.audit.route_helper import AuditChainContinuityError

    class _Backend:
        audit_chain = None

    with pytest.raises(AuditChainContinuityError):
        await write_audit_entry(
            _Backend(),
            None,
            op="archive",
            memory_id_str="mem_no_chain_required",
            content="x",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
            writer_id="alice",
            session_secret=b"x" * 32,
            required=True,
        )


@pytest.mark.asyncio
async def test_write_audit_entry_default_still_swallows(sqlite_backend, monkeypatch):
    """Regression guard: best-effort (default) callers must NOT see the
    new required-mode propagation — the F16 split is opt-in only."""
    from mnemos.audit import write_audit_entry

    async def _boom(*a, **kw):
        raise RuntimeError("simulated backend hiccup")

    monkeypatch.setattr(
        sqlite_backend.audit_chain,
        "insert_audit_entry",
        _boom,
    )

    # Default mode: must NOT raise
    async with sqlite_backend.transactional() as tx:
        await write_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str="mem_default_still_best_effort",
            content="x",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
            writer_id="alice",
            session_secret=b"x" * 32,
        )


def test_archive_snapshot_fetches_embedding_field():
    """F16: the route-side audit bridge for archive was passing
    ``embedding=None`` into ``write_audit_entry``, silently dropping
    the embedding field from the archive audit entry's payload_hash.

    The fix widens ``fetch_memory_archive_snapshot`` to include the
    ``embedding`` column. This regression test pins that the column is
    in the SELECT list — caught by reading the AST of the repo method.
    """
    import ast as _ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "mnemos" / "domain" / "admin_lifecycle_repo.py").read_text()
    tree = _ast.parse(source)

    found_method = False
    for node in _ast.walk(tree):
        if isinstance(node, _ast.AsyncFunctionDef) and node.name == "fetch_memory_archive_snapshot":
            found_method = True
            sql_text = _ast.unparse(node)
            # Both branches (sqlite portable + asyncpg) must include
            # the embedding column in their SELECT lists.
            assert sql_text.count("embedding") >= 2, (
                "fetch_memory_archive_snapshot must SELECT `embedding` from "
                "memories on BOTH the portable-sqlite and asyncpg branches "
                "(F16 fix: archive audit chain entry now hashes the real "
                "embedding instead of hardcoded None)"
            )
            # And the column must be the 5th SELECT-list element.
            assert "content, category, subcategory, metadata, embedding" in sql_text, (
                sql_text
            )
    assert found_method, "fetch_memory_archive_snapshot not found in admin_lifecycle_repo"


@pytest.mark.asyncio
async def test_archive_audit_entry_includes_real_embedding(sqlite_backend, monkeypatch):
    """F16 end-to-end: when an archive audit entry is written via the
    route helper, the embedding field carried in the payload_hash
    matches the real embedding bytes (not None).

    Pinned via a monkey-patched ``canonical_payload_hash`` spy: it
    records the ``embedding=`` it actually saw, and the test asserts
    the bytes match the snapshot.
    """
    import importlib

    from mnemos.audit import write_audit_entry

    spy = {}

    real_payload_hash = importlib.import_module(
        "mnemos.audit.crypto"
    ).canonical_payload_hash

    def _spy(*args, **kwargs):
        spy["embedding"] = kwargs.get("embedding")
        return real_payload_hash(*args, **kwargs)

    monkeypatch.setattr(
        "mnemos.audit.route_helper.canonical_payload_hash", _spy
    )

    embedding_bytes = b"\x01\x02\x03\x04\x05" * 32
    mid = "mem_1779637500099_archive_real_emb"

    async with sqlite_backend.transactional() as tx:
        await write_audit_entry(
            sqlite_backend,
            tx,
            op="archive",
            memory_id_str=mid,
            content="important archive",
            category="facts",
            subcategory=None,
            metadata={"k": "v"},
            embedding=embedding_bytes,
            writer_id="alice",
            session_secret=b"x" * 32,
        )

    assert spy.get("embedding") == embedding_bytes, (
        f"expected payload_hash to be signed over the real embedding "
        f"({embedding_bytes!r}), got {spy.get('embedding')!r}"
    )
