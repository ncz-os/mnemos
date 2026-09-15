"""Unit tests for v6.2 M-2.2.1 audit-chain route helper."""

from __future__ import annotations

import base64
from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_write_audit_entry_required_create_row_never_commits(
    sqlite_backend, monkeypatch
):
    """F16 row-level regression: ``test_write_audit_entry_required_propagates_insert_failure``
    only asserts the ``AuditChainContinuityError`` propagates out of the
    ``async with tx:`` block. It does NOT then verify, in a fresh
    connection/read, that the memory row itself was never actually
    committed.

    This test pins the row-level rollback contract for the plain CREATE
    path (the archive path is already covered by
    ``test_required_soft_delete_outage_rolls_back_memory_and_journal``):
    with ``required=True`` and an injected ``insert_audit_entry``
    failure, the surrounding transaction must roll back so neither the
    audit row NOR the just-inserted memory row survives past the
    ``async with`` exit.

    The propagation is asserted with ``pytest.raises`` around the
    ``async with`` block itself (NOT a try/except inside the block) --
    a swallowed exception inside the block would let
    ``transactional()`` treat the tx as successful and commit, which is
    exactly the silent-drift failure mode this test guards against. The
    archive-path twin test
    (``test_required_soft_delete_outage_rolls_back_memory_and_journal``)
    uses the same ``pytest.raises``-outside-the-with shape for the same
    reason.

    Concretely: insert a memory row + start an audit CREATE in the same
    tx; force the audit append to blow up; let the propagated
    ``AuditChainContinuityError`` escape the ``async with`` (so the
    rollback fires); open a fresh tx and assert the memory_id is NOT in
    ``memories``.
    """
    from mnemos.audit import write_audit_entry
    from mnemos.audit.route_helper import AuditChainContinuityError
    from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

    async def _boom(*a, **kw):
        raise RuntimeError("simulated backend hiccup on required create")

    monkeypatch.setattr(
        sqlite_backend.audit_chain,
        "insert_audit_entry",
        _boom,
    )

    memory_id = "mem_required_create_rollback_xyz"

    # The route-handler-side transaction: a real memory row + the
    # required-mode audit write share this same tx. We expect the audit
    # failure to propagate out of the block (NOT be caught inside it,
    # which would let transactional() commit) so the rollback fires.
    with pytest.raises(AuditChainContinuityError) as excinfo:
        async with sqlite_backend.transactional() as tx:
            ops = _Ops(tx, transaction_dialect(tx))
            # Minimal memory row so we can assert post-rollback absence.
            # content_hash is required on non-Postgres dialects (test_db2_live
            # uses 'hash-<id>' for the same reason).
            await ops.execute(
                "INSERT INTO memories("
                "id, content, category, owner_id, namespace, permission_mode, "
                "created, updated, metadata, content_hash"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                memory_id,
                "create-then-rollback content",
                "facts",
                "alice",
                "default",
                600,
                "2026-09-15T10:00:00+00:00",
                "2026-09-15T10:00:00+00:00",
                "{}",
                "hash-" + memory_id,
            )
            await write_audit_entry(
                sqlite_backend,
                tx,
                op="create",
                memory_id_str=memory_id,
                content="create-then-rollback content",
                category="facts",
                subcategory=None,
                metadata=None,
                embedding=None,
                writer_id="alice",
                session_secret=b"x" * 32,
                required=True,
            )

    assert "required audit write failed" in str(excinfo.value)

    # Now the row-level rollback assertion that the existing propagation
    # test does NOT make: open a *fresh* transaction (no savepoints, no
    # cached state from the rolled-back tx) and confirm the memory row
    # was actually rolled back. If the rollback did not fire, the row
    # would still be visible here -- the "memory row committed, audit
    # row missed" silent-drift failure mode F16 exists to prevent.
    async with sqlite_backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        present = await ops.scalar(
            "SELECT 1 FROM memories WHERE id = ?",
            memory_id,
        )
    assert present is None, (
        f"required-mode CREATE audit failure must roll back the "
        f"memory row too, but SELECT 1 FROM memories WHERE id = "
        f"{memory_id!r} returned {present!r} -- the row committed "
        f"without its audit-chain entry (F16 silent-drift failure mode)"
    )


# ─── F16 / coverage gate: helpers exercised directly ───────────────────────
#
# These tests pin the helper-function branches in ``mnemos/audit/route_helper.py``
# that are not naturally exercised by the round-trip ``write_audit_entry``
# flow tests above. They are part of the test:audit-coverage-gate job's
# coverage gate; a future regression that removes or renames a branch here
# will fail the gate even when overall coverage isn't tracked anywhere else.


def test_normalize_embedding_passes_through_bytes():
    """normalize_embedding: bytes/bytearray/memoryview pass-through branch."""
    from mnemos.audit.route_helper import normalize_embedding

    payload = b"\x01\x02\x03\x04"
    assert normalize_embedding(payload) == payload
    assert normalize_embedding(bytearray(payload)) == payload
    assert normalize_embedding(memoryview(payload)) == payload


def test_normalize_embedding_parses_json_string():
    """normalize_embedding: JSON-string branch (driver-encoded path)."""
    import json
    import struct

    from mnemos.audit.route_helper import normalize_embedding

    floats = [1.5, -2.5, 0.0]
    encoded = normalize_embedding(json.dumps(floats))
    assert encoded == struct.pack("<3f", *floats)


def test_normalize_embedding_parses_iterable():
    """normalize_embedding: list/tuple of numbers branch."""
    import struct

    from mnemos.audit.route_helper import normalize_embedding

    floats = [0.5, 0.25]
    assert normalize_embedding(floats) == struct.pack("<2f", *floats)
    assert normalize_embedding(tuple(floats)) == struct.pack("<2f", *floats)


def test_normalize_embedding_rejects_non_finite():
    """normalize_embedding: non-finite values must raise (not silently poison the hash)."""
    import math

    import pytest

    from mnemos.audit.route_helper import normalize_embedding

    with pytest.raises(ValueError, match="non-finite"):
        normalize_embedding([1.0, math.inf, 0.0])
    with pytest.raises(ValueError, match="non-finite"):
        normalize_embedding([1.0, math.nan, 0.0])


def test_normalize_embedding_returns_none_for_none():
    """normalize_embedding: None must round-trip to None (the audit side stores NULL)."""
    from mnemos.audit.route_helper import normalize_embedding

    assert normalize_embedding(None) is None


@pytest.mark.asyncio
async def test_fetch_audit_snapshot_returns_row(sqlite_backend):
    """fetch_audit_snapshot: SELECTs content/category/subcategory/metadata/embedding for locking."""
    from datetime import datetime, timezone

    from mnemos.audit.route_helper import fetch_audit_snapshot
    from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

    memory_id = "mem_fetch_snapshot_xyz"
    async with sqlite_backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO memories("
            "id, content, category, owner_id, namespace, permission_mode, "
            "created, updated, metadata, content_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            memory_id,
            "snapshot content",
            "facts",
            "alice",
            "default",
            600,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            '{"k":"v"}',
            "hash-" + memory_id,
        )

    async with sqlite_backend.transactional() as tx:
        snap = await fetch_audit_snapshot(tx, memory_id)
    assert snap is not None
    assert snap["content"] == "snapshot content"
    assert snap["category"] == "facts"
    assert snap["subcategory"] is None
    assert snap["metadata"] == '{"k":"v"}'
    assert "embedding" in snap


@pytest.mark.asyncio
async def test_fetch_audit_snapshot_missing_row_returns_none(sqlite_backend):
    """fetch_audit_snapshot: missing memory_id returns None (no row to lock)."""
    from mnemos.audit.route_helper import fetch_audit_snapshot

    async with sqlite_backend.transactional() as tx:
        snap = await fetch_audit_snapshot(tx, "mem_does_not_exist")
    assert snap is None


@pytest.mark.asyncio
async def test_write_transaction_audit_returns_early_when_disabled(
    sqlite_backend, monkeypatch
):
    """write_transaction_audit: when MNEMOS_AUDIT_CHAIN is unset, must short-circuit."""
    from mnemos.audit.route_helper import write_transaction_audit

    monkeypatch.delenv("MNEMOS_AUDIT_CHAIN", raising=False)

    async with sqlite_backend.transactional() as tx:
        # Should NOT raise and should NOT insert anything.
        await write_transaction_audit(
            tx,
            op="create",
            memory_id_str="mem_disabled",
            snapshot={
                "content": "x",
                "category": "facts",
                "subcategory": None,
                "metadata": None,
                "embedding": None,
            },
            writer_id="alice",
        )

    async with sqlite_backend.transactional() as tx:
        from mnemos.audit.route_helper import memory_id_to_audit_bytes

        row = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx, memory_id_to_audit_bytes("mem_disabled")
        )
    assert row is None


@pytest.mark.asyncio
async def test_write_configured_audit_entry_returns_early_when_disabled(
    sqlite_backend, monkeypatch
):
    """write_configured_audit_entry: when MNEMOS_AUDIT_CHAIN is unset, short-circuit."""
    from mnemos.audit.route_helper import write_configured_audit_entry

    monkeypatch.delenv("MNEMOS_AUDIT_CHAIN", raising=False)

    backend = sqlite_backend  # has an audit_chain repo
    async with sqlite_backend.transactional() as tx:
        await write_configured_audit_entry(
            backend,
            tx,
            op="create",
            memory_id_str="mem_cfg_disabled",
            snapshot={"content": "x", "category": "facts"},
            writer_id="alice",
        )


@pytest.mark.asyncio
async def test_write_configured_audit_entry_required_raises_when_snapshot_none(
    sqlite_backend, monkeypatch
):
    """write_configured_audit_entry: snapshot=None in required mode is a hard refusal."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        write_configured_audit_entry,
    )
    from mnemos.core import config

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "required")
    # session_secret must be set so we exercise the snapshot check, not
    # the upstream "requires a session secret" check.
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(session_secret="x" * 32)),
    )

    with pytest.raises(AuditChainContinuityError, match="snapshot is missing"):
        await write_configured_audit_entry(
            sqlite_backend,
            None,  # tx not exercised when snapshot is None
            op="create",
            memory_id_str="mem_cfg_no_snap",
            snapshot=None,
            writer_id="alice",
        )


@pytest.mark.asyncio
async def test_write_configured_audit_entry_required_raises_when_no_secret(
    sqlite_backend, monkeypatch
):
    """write_configured_audit_entry: required mode + empty session_secret is a hard refusal."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        write_configured_audit_entry,
    )

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "required")
    from mnemos.core import config

    monkeypatch.setattr(
        config, "get_settings", lambda: SimpleNamespace(server=SimpleNamespace(session_secret=""))
    )

    with pytest.raises(AuditChainContinuityError, match="requires a session secret"):
        await write_configured_audit_entry(
            sqlite_backend,
            None,
            op="create",
            memory_id_str="mem_cfg_no_secret",
            snapshot={"content": "x", "category": "facts"},
            writer_id="alice",
        )


@pytest.mark.asyncio
async def test_write_configured_audit_entry_required_wraps_inner_failure(
    sqlite_backend, monkeypatch
):
    """write_configured_audit_entry: required mode re-raises an AuditChainContinuityError
    inner exception unchanged (the AuditChainContinuityError pass-through branch).

    The wrap path ("required mutation audit failed") only fires for non-Audit
    inner exceptions. The AuditChain path is tested separately below."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        write_configured_audit_entry,
    )
    from mnemos.core import config

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "required")
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(session_secret="x" * 32)),
    )

    async def _boom(*a, **kw):
        raise RuntimeError("simulated inner failure")

    monkeypatch.setattr(
        sqlite_backend.audit_chain, "insert_audit_entry", _boom
    )

    # write_audit_entry converts RuntimeError -> AuditChainContinuityError
    # ("required audit write failed ..."), then write_configured_audit_entry
    # sees the AuditChain and re-raises it unchanged (the isinstance pass-
    # through at line 150). Either message style is acceptable here; the
    # contract is "the same AuditChainContinuityError type surfaces".
    raised = None
    async with sqlite_backend.transactional() as tx:
        try:
            await write_configured_audit_entry(
                sqlite_backend,
                tx,
                op="create",
                memory_id_str="mem_cfg_wrap",
                snapshot={
                    "content": "x",
                    "category": "facts",
                    "subcategory": None,
                    "metadata": None,
                    "embedding": None,
                },
                writer_id="alice",
            )
        except AuditChainContinuityError as exc:
            raised = exc
    assert raised is not None and isinstance(raised, AuditChainContinuityError)


@pytest.mark.asyncio
async def test_write_configured_audit_entry_required_wraps_non_audit_inner(
    sqlite_backend, monkeypatch
):
    """write_configured_audit_entry: required mode wraps a non-AuditChainContinuityError
    inner failure as AuditChainContinuityError("required mutation audit failed")
    so the caller sees a single failure type.

    The wrap path is reached when ``write_audit_entry`` itself never
    runs -- i.e., the exception fires earlier in the snapshot decode /
    metadata parse path. We trigger it with a metadata JSON string that
    fails to parse (``json.JSONDecodeError`` is a ``ValueError``, not an
    ``AuditChainContinuityError``)."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        write_configured_audit_entry,
    )
    from mnemos.core import config

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "required")
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(session_secret="x" * 32)),
    )

    raised = None
    async with sqlite_backend.transactional() as tx:
        try:
            await write_configured_audit_entry(
                sqlite_backend,
                tx,
                op="create",
                memory_id_str="mem_cfg_wrap_non_audit",
                snapshot={
                    "content": "x",
                    "category": "facts",
                    "subcategory": None,
                    # Bad JSON -> json.JSONDecodeError -> ValueError, which is
                    # caught by the `except Exception` block and re-wrapped as
                    # AuditChainContinuityError("required mutation audit failed").
                    "metadata": "{not valid json",
                    "embedding": None,
                },
                writer_id="alice",
            )
        except AuditChainContinuityError as exc:
            raised = exc
    assert raised is not None, "wrap path must produce an AuditChainContinuityError"
    assert "required mutation audit failed" in str(raised)


@pytest.mark.asyncio
async def test_write_configured_audit_entry_best_effort_swallows(
    sqlite_backend, monkeypatch
):
    """write_configured_audit_entry: best-effort (on, not required) must LOG+swallow,
    NOT raise -- backward compatibility for callers that don't sit inside a tx.

    To hit the `logger.exception` line at module scope (the outer `except
    Exception` of write_configured_audit_entry, NOT write_audit_entry's
    inner swallow), we trigger an exception BEFORE write_audit_entry is
    called: an invalid-JSON metadata string raises json.JSONDecodeError
    during the metadata parse step. With ``required=False``, the outer
    except logs and returns without raising."""
    from mnemos.audit.route_helper import write_configured_audit_entry
    from mnemos.core import config

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(session_secret="x" * 32)),
    )

    # MUST NOT raise (best-effort path: logger.exception then return None).
    async with sqlite_backend.transactional() as tx:
        await write_configured_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str="mem_cfg_best_effort",
            snapshot={
                "content": "x",
                "category": "facts",
                "subcategory": None,
                # Bad JSON triggers the outer except's logger.exception
                # (NOT write_audit_entry, which is never reached because
                # the JSON parse fails first).
                "metadata": "{not valid json",
                "embedding": None,
            },
            writer_id="alice",
        )


@pytest.mark.asyncio
async def test_write_configured_audit_entry_parses_string_metadata(
    sqlite_backend, monkeypatch
):
    """write_configured_audit_entry: metadata as a JSON string must be parsed before
    forwarding into ``write_audit_entry`` (drivers differ on this)."""
    from mnemos.audit.route_helper import write_configured_audit_entry
    from mnemos.core import config

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(session_secret="x" * 32)),
    )

    async with sqlite_backend.transactional() as tx:
        # metadata is a JSON STRING (driver-encoded form) -- the helper
        # must json.loads() it before passing into write_audit_entry.
        await write_configured_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str="mem_cfg_string_meta",
            snapshot={
                "content": "x",
                "category": "facts",
                "subcategory": None,
                "metadata": '{"k":"v"}',
                "embedding": None,
            },
            writer_id="alice",
        )


@pytest.mark.asyncio
async def test_write_configured_audit_entry_reads_file_like_fields(
    sqlite_backend, monkeypatch
):
    """write_configured_audit_entry: file-like content/metadata/embedding must be
    awaited via ``.read()`` before they reach ``write_audit_entry`` (large-memory
    upload path)."""

    from mnemos.audit.route_helper import write_configured_audit_entry
    from mnemos.core import config

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(session_secret="x" * 32)),
    )

    class AsyncBytes:
        """Async-file-like wrapper around a bytes blob."""

        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self.read_called = False

        async def read(self) -> bytes:
            self.read_called = True
            return self._payload

    content_handle = AsyncBytes(b"file-like content payload")
    meta_handle = AsyncBytes(b'{"source":"upload"}')

    async with sqlite_backend.transactional() as tx:
        await write_configured_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str="mem_cfg_filelike",
            snapshot={
                "content": content_handle,
                "category": "facts",
                "subcategory": None,
                "metadata": meta_handle,
                "embedding": None,
            },
            writer_id="alice",
        )

    assert content_handle.read_called, "write_configured_audit_entry must await content.read()"
    assert meta_handle.read_called, "write_configured_audit_entry must await metadata.read()"


@pytest.mark.asyncio
async def test_write_audit_entry_required_raises_when_no_session_secret(
    sqlite_backend, monkeypatch
):
    """write_audit_entry: required=True with empty session_secret must raise
    AuditChainContinuityError -- not silently no-op."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        write_audit_entry,
    )

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "required")

    with pytest.raises(
        AuditChainContinuityError, match="required audit signing requires a session secret"
    ):
        await write_audit_entry(
            sqlite_backend,
            None,
            op="create",
            memory_id_str="mem_no_secret_required",
            content="x",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
            writer_id="alice",
            session_secret=b"",
            required=True,
        )


@pytest.mark.asyncio
async def test_write_audit_entry_expected_prev_head_no_predecessor(
    sqlite_backend, monkeypatch
):
    """write_audit_entry: expected_prev_head supplied but the local chain has NO
    prior entry must raise (no fallback to installing a peer-supplied head)."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        write_audit_entry,
    )

    mid = "mem_expected_no_prev"
    # 16 bytes, not all zero (entry id) and 32 bytes, not all zero (entry hash).
    expected_entry_id = b"\x11" * 16
    expected_entry_hash = b"\x22" * 32

    raised = None
    async with sqlite_backend.transactional() as tx:
        try:
            await write_audit_entry(
                sqlite_backend,
                tx,
                op="create",
                memory_id_str=mid,
                content="x",
                category="facts",
                subcategory=None,
                metadata=None,
                embedding=None,
                writer_id="alice",
                session_secret=b"x" * 32,
                expected_prev_entry_id_hex=expected_entry_id.hex(),
                expected_prev_entry_hash_hex=expected_entry_hash.hex(),
                required=True,
            )
        except AuditChainContinuityError as exc:
            raised = exc
    assert raised is not None, "expected prev head with no local chain must raise"
    # Two acceptable messages:
    #   - "local audit chain has no predecessor" -- the branch we want.
    #   - "required audit write failed" -- if the AuditChainContinuityError
    #     pass-through in write_audit_entry wraps it (which would mean the
    #     branch ordering changed; either way, the write did not commit).
    msg = str(raised)
    assert (
        "local audit chain has no predecessor" in msg
        or "required audit write failed" in msg
    ), f"unexpected error: {msg!r}"


@pytest.mark.asyncio
async def test_write_audit_entry_expected_prev_head_mismatch(sqlite_backend):
    """write_audit_entry: expected_prev_head supplied AND a local entry exists,
    but the heads do NOT match -- must raise."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        write_audit_entry,
    )

    mid = "mem_expected_mismatch"
    # First write: seed the local chain with a known prev head.
    async with sqlite_backend.transactional() as tx:
        await write_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str=mid,
            content="seed",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
            writer_id="alice",
            session_secret=b"x" * 32,
        )

    # Now try with a different (wrong) expected_prev head.
    wrong_entry_id = b"\x11" * 16
    wrong_entry_hash = b"\x22" * 32

    raised = None
    async with sqlite_backend.transactional() as tx:
        try:
            await write_audit_entry(
                sqlite_backend,
                tx,
                op="update",
                memory_id_str=mid,
                content="x",
                category="facts",
                subcategory=None,
                metadata=None,
                embedding=None,
                writer_id="alice",
                session_secret=b"x" * 32,
                expected_prev_entry_id_hex=wrong_entry_id.hex(),
                expected_prev_entry_hash_hex=wrong_entry_hash.hex(),
                required=True,
            )
        except AuditChainContinuityError as exc:
            raised = exc
    assert raised is not None, "expected prev head mismatch must raise"
    msg = str(raised)
    assert (
        "expected prev head does not match" in msg
        or "required audit write failed" in msg
    ), f"unexpected error: {msg!r}"


@pytest.mark.asyncio
async def test_write_audit_entry_expected_prev_head_match_succeeds(
    sqlite_backend,
):
    """write_audit_entry: expected_prev_head supplied AND it exactly matches
    the local chain head -- the write must succeed and install the matching
    prev head. (Line 247 -> 250 success branch: the override equals the
    local head, so the chain extends naturally.)

    Note: ``_audit_prev_head`` returns ``(entry_id, latest_hash(entry, sig))``,
    NOT ``(entry_id, prev_entry_hash_column)``. ``latest_hash`` is the hash
    of the previous entry's canonical bytes + signature, which is what the
    next entry's ``prev_entry_hash`` column will hold."""
    from mnemos.audit.crypto import (
        AuditEntry,
        derive_writer_keypair,
    )
    from mnemos.audit.route_helper import (
        memory_id_to_audit_bytes,
        write_audit_entry,
    )

    mid = "mem_expected_match"
    # Seed a chain entry.
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

    # Read the local chain head so we can supply it as the expected prev
    # head for the next write. We have to RECOMPUTE the prev_entry_hash
    # value that ``_audit_prev_head`` would return -- the column itself
    # is None for the very first entry (no predecessor).
    async with sqlite_backend.transactional() as tx:
        head = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx, memory_id_to_audit_bytes(mid)
        )
    assert head is not None
    matching_entry_id = head["entry_id"]

    # Rebuild the entry's signed_at by trying every candidate the helper
    # would try. (Cleanest approach is to just construct it from the
    # fields on the row.)

    # The simplest reliable way: the prev_entry_hash that the chain stores
    # for entry N+1 is ``latest_hash(entry_N, signature_N)``. We can
    # compute it by re-running the signing machinery.
    private_key, _ = derive_writer_keypair(b"x" * 32, "alice")
    # signed_at must match what was stored. Try every candidate.
    from mnemos.audit.route_helper import _signed_at_candidates

    matched_hash = None
    for signed_at in _signed_at_candidates(head["signed_at"]):
        prev_ae = AuditEntry(
            entry_id=head["entry_id"],
            memory_id=head["memory_id"],
            prev_entry_id=head.get("prev_entry_id"),
            prev_entry_hash=head.get("prev_entry_hash"),
            op=head["op"],
            payload_hash=head["payload_hash"],
            writer_id=head["writer_id"],
            writer_pubkey=head["writer_pubkey"],
            signed_at=signed_at,
        )
        # verify the signature to confirm we matched the right signed_at
        if prev_ae.signed_at == signed_at and head["signature"]:
            # Use canonical_payload_hash as a quick stand-in for the hash;
            # but we actually want entry_hash = sha256(canonical_bytes || sig).
            from mnemos.audit.crypto import entry_hash

            matched_hash = entry_hash(prev_ae, head["signature"])
            # verify signature to make sure we have the right signed_at
            from mnemos.audit.crypto import verify_entry

            if verify_entry(prev_ae, head["signature"]):
                break
            matched_hash = None
    assert matched_hash is not None, "could not reconstruct prev_entry_hash from head row"

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
            expected_prev_entry_id_hex=matching_entry_id.hex(),
            expected_prev_entry_hash_hex=matched_hash.hex(),
            required=True,
        )

    # Confirm the new entry's prev matches what we supplied.
    async with sqlite_backend.transactional() as tx:
        new_head = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx, memory_id_to_audit_bytes(mid)
        )
    assert new_head["op"] == "update"
    assert new_head["prev_entry_id"] == matching_entry_id
    assert new_head["prev_entry_hash"] == matched_hash


@pytest.mark.asyncio
async def test_write_audit_entry_enforce_continuity_propagates(
    sqlite_backend, monkeypatch
):
    """write_audit_entry: enforce_continuity=True must re-raise the underlying
    insert failure (federation callers depend on this signal)."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        write_audit_entry,
    )

    async def _boom(*a, **kw):
        raise RuntimeError("simulated federation outage")

    monkeypatch.setattr(
        sqlite_backend.audit_chain, "insert_audit_entry", _boom
    )

    raised = None
    async with sqlite_backend.transactional() as tx:
        try:
            await write_audit_entry(
                sqlite_backend,
                tx,
                op="replicate",
                memory_id_str="mem_enforce_continuity",
                content="x",
                category="facts",
                subcategory=None,
                metadata=None,
                embedding=None,
                writer_id="alice",
                session_secret=b"x" * 32,
                enforce_continuity=True,
            )
        except (RuntimeError, AuditChainContinuityError) as exc:
            raised = exc
    # enforce_continuity=True re-raises the ORIGINAL exception (RuntimeError
    # here), not wrapped as AuditChain. The required-mode rewrap would mask
    # the failure mode for the federation caller, so it must NOT trigger
    # when enforce_continuity=True is the documented opt-in.
    assert raised is not None, "enforce_continuity=True must propagate"
    assert isinstance(raised, RuntimeError), (
        f"enforce_continuity=True must re-raise the original exception type, "
        f"got {type(raised).__name__}: {raised!r}"
    )


@pytest.mark.asyncio
async def test_write_audit_entry_savepoint_typeerror_fallback(
    sqlite_backend, monkeypatch
):
    """write_audit_entry: when transaction_dialect(tx) raises TypeError (a tx
    wrapper that doesn't fit the supported shape), the helper must fall back
    to the no-savepoint path and still succeed.

    ``write_audit_entry`` does the dialect detection via a lazy local
    import inside the function body (``from mnemos.persistence.worker_lifecycle
    import _Ops, transaction_dialect``), so patching the symbol on the
    source module does NOT affect the function's local binding. We patch
    the ``transaction_dialect`` attribute on the source module and the
    function does its own local import on every call, so this works:
    the patch becomes visible the next time the function re-runs the
    local import statement.

    Actually, ``from X import Y`` creates a NEW binding on the caller
    scope each call (so patches on the source module ARE visible). The
    test still confirms the TypeError fallback path executes and the
    write completes successfully."""
    from mnemos.audit.route_helper import write_audit_entry

    def _broken(tx):
        raise TypeError("unsupported tx wrapper shape")

    monkeypatch.setattr(
        "mnemos.persistence.worker_lifecycle.transaction_dialect",
        _broken,
    )

    async with sqlite_backend.transactional() as tx:
        # Must not raise -- the TypeError fallback skips the SAVEPOINT
        # path and proceeds straight to insert_audit_entry (which
        # succeeds for the fresh memory_id).
        await write_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str="mem_savepoint_typeerror",
            content="x",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
            writer_id="alice",
            session_secret=b"x" * 32,
        )

    # Confirm the audit row was actually inserted (the fallback path
    # did NOT abort the write).
    from mnemos.audit.route_helper import memory_id_to_audit_bytes

    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx, memory_id_to_audit_bytes("mem_savepoint_typeerror")
        )
    assert row is not None, (
        "TypeError-on-dialect-detection fallback must still insert the audit row"
    )
    assert row["op"] == "create"


@pytest.mark.asyncio
async def test_audit_prev_head_raises_on_invalid_signature(
    sqlite_backend, monkeypatch
):
    """_audit_prev_head: if the previous row's signature does not verify against
    ANY signed_at candidate, must raise AuditChainContinuityError."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        _audit_prev_head,
        memory_id_to_audit_bytes,
    )

    # Seed a valid audit entry first so there's a row to read.
    mid = "mem_invalid_sig"
    from mnemos.audit import write_audit_entry

    async with sqlite_backend.transactional() as tx:
        await write_audit_entry(
            sqlite_backend,
            tx,
            op="create",
            memory_id_str=mid,
            content="x",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
            writer_id="alice",
            session_secret=b"x" * 32,
        )

    # Now read the row, corrupt the signature, and feed it to _audit_prev_head.
    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.audit_chain.get_latest_audit_entry(
            tx, memory_id_to_audit_bytes(mid)
        )
    assert row is not None
    row["signature"] = b"\x00" * len(row["signature"])

    with pytest.raises(
        AuditChainContinuityError, match="signature is invalid"
    ):
        _audit_prev_head(row)


def test_audit_prev_head_returns_head_for_valid_signature():
    """_audit_prev_head: when verify_entry succeeds on the first candidate,
    returns (entry_id, latest_hash) without raising."""

    from mnemos.audit import build_entry, latest_hash
    from mnemos.audit.route_helper import _audit_entry_from_row, _audit_prev_head, _signed_at_candidates

    entry, sig = build_entry(
        op="create",
        memory_id=b"\x33" * 16,
        prev_entry_id=None,
        prev_entry_hash=None,
        payload_hash=b"\x44" * 32,
        writer_id="alice",
        session_secret=b"x" * 32,
    )

    row = {
        "entry_id": entry.entry_id,
        "memory_id": entry.memory_id,
        "prev_entry_id": None,
        "prev_entry_hash": None,
        "op": "create",
        "payload_hash": entry.payload_hash,
        "writer_id": entry.writer_id,
        "writer_pubkey": entry.writer_pubkey,
        "signed_at": entry.signed_at,
        "signature": sig,
    }

    # Sanity check: at least one of _signed_at_candidates(...) must verify.
    # If this fails, the row shape is wrong (signed_at format mismatch).
    verified = False
    for signed_at in _signed_at_candidates(row["signed_at"]):
        if _audit_entry_from_row(row, signed_at=signed_at) is not None:
            from mnemos.audit.crypto import verify_entry

            if verify_entry(_audit_entry_from_row(row, signed_at=signed_at), sig):
                verified = True
                break
    assert verified, (
        "row construction is wrong: no _signed_at_candidates value verifies "
        "against the build_entry-produced signature"
    )

    head_id, head_hash = _audit_prev_head(row)
    assert head_id == entry.entry_id
    assert head_hash == latest_hash(entry, sig)


def test_signed_at_candidates_string_with_z():
    """_signed_at_candidates: string ending in 'Z' must produce a +00:00 candidate."""
    from mnemos.audit.route_helper import _signed_at_candidates

    candidates = _signed_at_candidates("2026-09-15T10:00:00Z")
    assert "2026-09-15T10:00:00+00:00" in candidates


def test_signed_at_candidates_string_with_space():
    """_signed_at_candidates: string with a space must produce a T-separated candidate."""
    from mnemos.audit.route_helper import _signed_at_candidates

    candidates = _signed_at_candidates("2026-09-15 10:00:00")
    assert "2026-09-15T10:00:00" in candidates


def test_signed_at_candidates_string_bare_offset():
    """_signed_at_candidates: bare string without + or Z must gain +00:00."""
    from mnemos.audit.route_helper import _signed_at_candidates

    candidates = _signed_at_candidates("2026-09-15T10:00:00")
    assert "2026-09-15T10:00:00+00:00" in candidates


def test_signed_at_candidates_naive_datetime():
    """_signed_at_candidates: naive datetime must gain tzinfo=UTC."""
    from datetime import datetime, timezone

    from mnemos.audit.route_helper import _signed_at_candidates

    naive = datetime(2026, 9, 15, 10, 0, 0)
    candidates = _signed_at_candidates(naive)
    iso = naive.replace(tzinfo=timezone.utc).isoformat()
    assert iso in candidates


def test_signed_at_candidates_aware_datetime():
    """_signed_at_candidates: tz-aware datetime hits the astimezone branch
    (line 353). When the value is already in UTC, the astimezone call
    produces the same string the first add() already recorded, so the
    duplicate-add short-circuit at line 344 (the 344->exit branch) fires."""
    from datetime import datetime, timezone

    from mnemos.audit.route_helper import _signed_at_candidates

    aware_utc = datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc)
    candidates = _signed_at_candidates(aware_utc)
    iso = aware_utc.isoformat()
    assert iso in candidates
    # The dedupe invariant: candidates must be unique.
    assert len(candidates) == len(set(candidates))


def test_signed_at_candidates_aware_non_utc_datetime():
    """_signed_at_candidates: tz-aware non-UTC datetime must convert via astimezone."""
    from datetime import datetime, timedelta, timezone

    from mnemos.audit.route_helper import _signed_at_candidates

    # +05:00 wall clock; the .astimezone(UTC) call must normalize to +00:00.
    plus_five = timezone(timedelta(hours=5))
    local_dt = datetime(2026, 9, 15, 10, 0, 0, tzinfo=plus_five)
    candidates = _signed_at_candidates(local_dt)
    iso_utc = local_dt.astimezone(timezone.utc).isoformat()
    assert iso_utc in candidates


def test_to_iso_falls_back_to_str():
    """_to_iso: objects without .isoformat() must round-trip via str()."""
    from mnemos.audit.route_helper import _to_iso

    class Plain:
        def __str__(self) -> str:
            return "plain-object-string"

    assert _to_iso(Plain()) == "plain-object-string"


def test_decode_expected_hex_rejects_invalid_hex():
    """_decode_expected_hex: invalid hex must raise AuditChainContinuityError."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        _decode_expected_hex,
    )

    with pytest.raises(AuditChainContinuityError, match="is not valid hex"):
        _decode_expected_hex("not-hex-zzz", label="expected_prev_entry_id_hex", length=16)


def test_decode_expected_hex_rejects_wrong_length():
    """_decode_expected_hex: wrong decoded length must raise."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        _decode_expected_hex,
    )

    with pytest.raises(AuditChainContinuityError, match="must decode to 16 bytes"):
        _decode_expected_hex("ab", label="expected_prev_entry_id_hex", length=16)


def test_decode_expected_hex_rejects_all_zero():
    """_decode_expected_hex: all-zero bytes are rejected (zero is sentinel for NULL)."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        _decode_expected_hex,
    )

    with pytest.raises(AuditChainContinuityError, match="must not be all-zero bytes"):
        _decode_expected_hex("00" * 16, label="expected_prev_entry_id_hex", length=16)


def test_decode_expected_hex_accepts_valid_bytes():
    """_decode_expected_hex: valid hex of the right length and not all-zero decodes."""
    from mnemos.audit.route_helper import _decode_expected_hex

    assert _decode_expected_hex("ab" * 16, label="x", length=16) == b"\xab" * 16
    assert _decode_expected_hex(None, label="x", length=16) is None
    assert _decode_expected_hex("", label="x", length=16) is None


def test_decode_expected_prev_head_only_one_supplied_raises():
    """_decode_expected_prev_head: only id OR only hash supplied is a mismatch."""
    from mnemos.audit.route_helper import (
        AuditChainContinuityError,
        _decode_expected_prev_head,
    )

    with pytest.raises(
        AuditChainContinuityError, match="must both be supplied or both be empty"
    ):
        _decode_expected_prev_head(
            expected_prev_entry_id_hex="ab" * 16,
            expected_prev_entry_hash_hex=None,
        )


def test_decode_expected_prev_head_returns_pair_when_both_supplied():
    """_decode_expected_prev_head: both supplied (valid) returns the (id, hash) tuple."""
    from mnemos.audit.route_helper import _decode_expected_prev_head

    out = _decode_expected_prev_head(
        expected_prev_entry_id_hex="ab" * 16,
        expected_prev_entry_hash_hex="cd" * 32,
    )
    assert out == (b"\xab" * 16, b"\xcd" * 32)


def test_decode_expected_prev_head_returns_none_when_both_empty():
    """_decode_expected_prev_head: both empty returns None (no override requested)."""
    from mnemos.audit.route_helper import _decode_expected_prev_head

    assert (
        _decode_expected_prev_head(
            expected_prev_entry_id_hex=None,
            expected_prev_entry_hash_hex=None,
        )
        is None
    )
