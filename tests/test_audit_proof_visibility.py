"""CF-22 (MN-06): /v1/audit/proof must not be a cross-tenant existence oracle.

Before the fix, ``audit_proof_head`` answered from ``memory_audit_chain``
alone: any authenticated caller who guessed/knew a memory id got the full
chain head (op, writer_id, signed_at, sealed state) for a memory that
``GET /v1/memories/{id}`` would 404 for them. The audit chain is the
forensic log of what EXISTS, so it leaks tenant existence even after a
memory row is deleted or archived.

Contract pinned here:

* Before returning the chain head the route resolves the memory through
  the caller's ``VisibilityFilter.for_read`` (same predicate as the
  memories GET-by-id read path: root bypasses, non-root is pinned to its
  namespace with the secret vault subtracted).
* Invisible memory -> 404 with the SAME body as a missing id, so
  "invisible to me" and "does not exist" are indistinguishable (no oracle).
* Visible memory -> full proof, exactly as before.
* Decisions (documented per the task):
  * ``include_secrets=True`` mirrors the memories GET-by-id explicit-fetch
    contract: root may fetch the audit proof of a vault-namespace memory by
    exact id; non-root never can (vault rows are subtracted even when
    targeted, so they 404).
  * A DELETED (soft-deleted) memory is invisible to EVERYONE (root
    included): the memories read predicate already excludes
    ``deleted_at IS NOT NULL`` rows, so the proof endpoint must too. The
    audit chain outlives the memory row by design (tombstones, GDPR
    deletion); making a dead row's forensic head readable again would
    re-open the oracle through the delete path. The proof of a deleted
    memory is an operator job (direct store access), not an API surface.
  * An ARCHIVED memory is still readable (``include_archived=True``
    mirrors GET-by-id): archiving is retention, not deletion.
  * ``/v1/audit/inclusion_proof`` is out of scope: it takes an entry_id
    that is only obtainable by already passing through this visibility
    check, so it is not an independent oracle.
"""

from __future__ import annotations

import asyncio
import base64

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from mnemos.core import config as _core_config

    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    monkeypatch.setenv(
        "MNEMOS_AUDIT_ROOT_PRIVKEY",
        base64.b64encode(b"\x42" * 32).decode(),
    )
    # write_configured_audit_entry / write_audit_entry read this live.
    monkeypatch.setenv("MNEMOS_SESSION_SECRET", "test-secret-32-bytes-long-padding-000")
    _core_config.reload_settings()
    yield
    _core_config.reload_settings()


def _make_user(role="user", user_id="u-1", ns="default"):
    from mnemos.api.dependencies import UserContext

    return UserContext(
        user_id=user_id,
        group_ids=[],
        role=role,
        namespace=ns,
        authenticated=True,
    )


def _sqlite_backend(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    class _S:
        class database:
            embedding_dim = 1024

    return SqliteBackend(tmp_path / "cf22.db", _S())


async def _write_memory(backend, *, memory_id, owner, namespace, permission_mode=600, archived_at=None):
    from datetime import datetime

    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content=f"content of {memory_id}",
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=3,
            owner_id=owner,
            namespace=namespace,
            permission_mode=permission_mode,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=None,
            created=None,
            updated=None,
        )
        if archived_at is not None:
            from mnemos.persistence.sqlite import _commit, _execute

            conn = tx.conn if hasattr(tx, "conn") else tx._conn
            await _execute(
                conn,
                "UPDATE memories SET archived_at = ? WHERE id = ?",
                (datetime.fromisoformat(archived_at).isoformat(), memory_id),
            )
            await _commit(conn)


async def _write_audit(backend, *, memory_id, writer, op="create"):
    """Append a real signed audit-chain entry through the route-side helper."""
    from mnemos.api.memory_mutations import _write_memory_mutation_audit_entry

    async with backend.transactional() as tx:
        await _write_memory_mutation_audit_entry(
            backend,
            tx,
            op=op,
            memory_id=memory_id,
            content=f"content of {memory_id}",
            category="facts",
            subcategory=None,
            metadata={},
            writer_id=writer,
        )


def _call(monkeypatch, backend, memory_id_str, user):
    import mnemos.api.persistence_helpers as _ph
    from mnemos.api.routes import audit as audit_route
    from types import SimpleNamespace

    monkeypatch.setattr(_ph, "_lc", SimpleNamespace(_persistence_backend=backend, _rls_enabled=False))
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(audit_route.audit_proof_head(memory_id_str=memory_id_str, user=user))
    finally:
        loop.close()


def test_audit_proof_requires_visibility(tmp_path, monkeypatch):
    """Non-root caller cannot use /v1/audit/proof to confirm another
    tenant's memory exists: the route must 404 exactly as if the id were
    unknown (same body -> no oracle)."""
    from fastapi import HTTPException

    backend = _sqlite_backend(tmp_path)

    async def _setup():
        await backend.open()
        await _write_memory(
            backend, memory_id="mem_bob_vault_note", owner="bob", namespace="bob-ns", permission_mode=600
        )
        await _write_audit(backend, memory_id="mem_bob_vault_note", writer="bob")

    asyncio.run(_setup())

    alice = _make_user(role="user", user_id="alice", ns="default")
    try:
        with pytest.raises(HTTPException) as exc:
            _call(monkeypatch, backend, "mem_bob_vault_note", alice)
        assert exc.value.status_code == 404
        # Same body as a missing id — indistinguishable, no existence leak.
        # (The route echoes the request's id in the detail; it must never
        # contain response material that differs between the two cases.)
        with pytest.raises(HTTPException) as exc2:
            _call(monkeypatch, backend, "mem_never_existed", alice)
        assert exc2.value.status_code == 404
        assert "mem_bob_vault_note" not in exc2.value.detail
        assert "mem_never_existed" not in exc.value.detail
    finally:
        asyncio.run(backend.close())


def test_audit_proof_visible_owner_still_gets_proof(tmp_path, monkeypatch):
    """The owner of a visible memory still gets the full chain head."""
    backend = _sqlite_backend(tmp_path)

    async def _setup():
        await backend.open()
        await _write_memory(
            backend, memory_id="mem_alice_note", owner="alice", namespace="default", permission_mode=644
        )
        await _write_audit(backend, memory_id="mem_alice_note", writer="alice")

    asyncio.run(_setup())

    alice = _make_user(role="user", user_id="alice", ns="default")
    try:
        out = _call(monkeypatch, backend, "mem_alice_note", alice)
        assert out["memory_id"] == "mem_alice_note"
        assert out["writer_id"] == "alice"
        assert out["op"] == "create"
        assert out["entry_id"]
        assert out["payload_hash"]
        assert out["sealed"] is False
    finally:
        asyncio.run(backend.close())


def test_audit_proof_root_reads_other_tenants(tmp_path, monkeypatch):
    """Root bypasses the visibility predicate (same contract as
    GET /v1/memories/{id}): root can fetch the proof for another
    tenant's live memory."""
    backend = _sqlite_backend(tmp_path)

    async def _setup():
        await backend.open()
        await _write_memory(backend, memory_id="mem_bob_root", owner="bob", namespace="bob-ns", permission_mode=600)
        await _write_audit(backend, memory_id="mem_bob_root", writer="bob")

    asyncio.run(_setup())

    root = _make_user(role="root", user_id="root", ns="default")
    try:
        out = _call(monkeypatch, backend, "mem_bob_root", root)
        assert out["memory_id"] == "mem_bob_root"
        assert out["writer_id"] == "bob"
    finally:
        asyncio.run(backend.close())


def test_audit_proof_deleted_memory_invisible_even_to_root(tmp_path, monkeypatch):
    """A soft-deleted memory's audit head is invisible to everyone,
    root included: the memories read predicate already excludes
    ``deleted_at IS NOT NULL`` rows, so the proof endpoint must too.
    (Documented decision — see module docstring.)"""
    from fastapi import HTTPException

    backend = _sqlite_backend(tmp_path)

    async def _setup():
        from datetime import datetime, timezone

        from mnemos.persistence.sqlite import _commit, _execute

        await backend.open()
        await _write_memory(backend, memory_id="mem_bob_deleted", owner="bob", namespace="bob-ns", permission_mode=600)
        await _write_audit(backend, memory_id="mem_bob_deleted", writer="bob")
        async with backend.transactional() as tx:
            conn = tx.conn if hasattr(tx, "conn") else tx._conn
            await _execute(
                conn,
                "UPDATE memories SET deleted_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), "mem_bob_deleted"),
            )
            await _commit(conn)

    asyncio.run(_setup())

    root = _make_user(role="root", user_id="root", ns="default")
    alice = _make_user(role="user", user_id="alice", ns="default")
    try:
        with pytest.raises(HTTPException) as exc_root:
            _call(monkeypatch, backend, "mem_bob_deleted", root)
        assert exc_root.value.status_code == 404
        with pytest.raises(HTTPException) as exc_alice:
            _call(monkeypatch, backend, "mem_bob_deleted", alice)
        assert exc_alice.value.status_code == 404
    finally:
        asyncio.run(backend.close())


def test_audit_proof_archived_memory_still_readable(tmp_path, monkeypatch):
    """Archiving is retention, not deletion: GET-by-id serves archived
    rows to the owner (include_archived), so the proof must as well."""
    backend = _sqlite_backend(tmp_path)

    async def _setup():
        await backend.open()
        await _write_memory(
            backend,
            memory_id="mem_alice_archived",
            owner="alice",
            namespace="default",
            permission_mode=644,
            archived_at="2026-11-01T00:00:00+00:00",
        )
        await _write_audit(backend, memory_id="mem_alice_archived", writer="alice", op="archive")

    asyncio.run(_setup())

    alice = _make_user(role="user", user_id="alice", ns="default")
    try:
        out = _call(monkeypatch, backend, "mem_alice_archived", alice)
        assert out["memory_id"] == "mem_alice_archived"
        assert out["op"] == "archive"
    finally:
        asyncio.run(backend.close())


def test_audit_proof_vault_memory_root_only(tmp_path, monkeypatch):
    """Vault-namespace row: root may fetch the proof by exact id
    (include_secrets=True, mirroring GET-by-id), non-root 404s even
    though the audit entry exists."""
    from fastapi import HTTPException
    from mnemos.core.secret_detection import VAULT_NAMESPACE

    backend = _sqlite_backend(tmp_path)

    async def _setup():
        await backend.open()
        await _write_memory(
            backend, memory_id="mem_vault_cred", owner="fleet", namespace=VAULT_NAMESPACE, permission_mode=600
        )
        await _write_audit(backend, memory_id="mem_vault_cred", writer="fleet")

    asyncio.run(_setup())

    root = _make_user(role="root", user_id="root", ns="default")
    alice = _make_user(role="user", user_id="alice", ns="default")
    try:
        out = _call(monkeypatch, backend, "mem_vault_cred", root)
        assert out["memory_id"] == "mem_vault_cred"
        with pytest.raises(HTTPException) as exc:
            _call(monkeypatch, backend, "mem_vault_cred", alice)
        assert exc.value.status_code == 404
    finally:
        asyncio.run(backend.close())
