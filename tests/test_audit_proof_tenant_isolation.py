"""MN-06 / CF-22: ``/v1/audit/proof`` must not be a cross-tenant existence oracle.

Background
----------
``GET /v1/audit/proof?memory_id_str=...`` looks a memory up in the audit
chain by its *audit key* and answered 200 with the memory's entry_id,
writer_id, signature and signed_at for **any** ``memory_id_str`` a caller
named -- including memories owned by another tenant. The endpoint never
consulted the memories visibility predicate, so a non-root caller could
distinguish:

* memory exists, owned by someone else  -> 200 + audit metadata
* memory does not exist                 -> 404

That is an existence oracle over the whole fleet, reachable with nothing
but a guessable memory id and a bearer token. CF-22's fix is the same
contract every other read endpoint already honours (``GET
/v1/memories/{id}``, ``GET /v1/dag/...``, ``GET /v1/memories/{id}/acl``):
load the memory under ``VisibilityFilter.for_read`` first and collapse
"not visible" into the same 404 as "not found".

These tests drive the real SQLite backend -- real ``memories`` rows,
real visibility predicate, real audit chain -- rather than a mock, so a
regression that quietly stops consulting visibility fails here.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

from mnemos.audit import build_entry, canonical_payload_hash
from mnemos.core.auth_context import UserContext
from mnemos.persistence.sqlite import SqliteBackend


@pytest.fixture(autouse=True)
def _audit_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_AUDIT_CHAIN", "on")
    monkeypatch.setenv(
        "MNEMOS_AUDIT_ROOT_PRIVKEY",
        base64.b64encode(b"\x42" * 32).decode(),
    )


@pytest_asyncio.fixture
async def backend(tmp_path):
    b = SqliteBackend(tmp_path / "proof-isolation.db", SimpleNamespace(database=SimpleNamespace(embedding_dim=8)))
    await b.open()
    yield b
    await b.close()


def _user(user_id: str, *, namespace: str = "default", role: str = "user") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role=role,
        namespace=namespace,
        authenticated=True,
    )


async def _seed_memory_with_audit_entry(backend, *, memory_id: str, owner_id: str, namespace: str) -> None:
    """Insert a private memory row plus one signed audit entry for it."""
    now = datetime.now(timezone.utc)
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content="tenant-secret",
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=80,
            owner_id=owner_id,
            namespace=namespace,
            permission_mode=600,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content="tenant-secret",
            created=now,
            updated=now,
        )

        from mnemos.audit.route_helper import memory_id_to_audit_bytes

        audit_key = memory_id_to_audit_bytes(memory_id)
        payload_hash = canonical_payload_hash(
            memory_id=memory_id,
            content="tenant-secret",
            category="facts",
            subcategory=None,
            metadata=None,
            embedding=None,
        )
        entry, sig = build_entry(
            op="create",
            memory_id=audit_key,
            prev_entry_id=None,
            prev_entry_hash=None,
            payload_hash=payload_hash,
            writer_id=owner_id,
            session_secret=b"x" * 32,
            signed_at=now,
        )
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


@pytest.fixture
def route(monkeypatch, backend):
    from mnemos.api.routes import audit as audit_route

    monkeypatch.setattr(audit_route, "audit_chain_enabled", lambda: True)
    monkeypatch.setattr(audit_route, "_backend_or_503", lambda: backend)
    return audit_route


@pytest.mark.asyncio
async def test_proof_hides_other_tenant_memory_behind_404(route, backend):
    """A caller must not learn that another tenant's memory has audit entries."""
    mem_id = f"mem_{uuid.uuid4().hex}"
    await _seed_memory_with_audit_entry(backend, memory_id=mem_id, owner_id="owner-alice", namespace="tenant-a")

    attacker = _user("attacker", namespace="tenant-b")

    with pytest.raises(HTTPException) as exc:
        await route.audit_proof_head(memory_id_str=mem_id, user=attacker)

    assert exc.value.status_code == 404
    # The detail must match the genuine "no such memory" case exactly, so the
    # response body itself is not an oracle.
    assert exc.value.detail == f"no audit entries for memory_id={mem_id}"


@pytest.mark.asyncio
async def test_proof_owner_in_same_namespace_still_reads(route, backend):
    """The fix must not lock the owning caller out of their own proof."""
    mem_id = f"mem_{uuid.uuid4().hex}"
    await _seed_memory_with_audit_entry(backend, memory_id=mem_id, owner_id="owner-bob", namespace="tenant-a")

    out = await route.audit_proof_head(memory_id_str=mem_id, user=_user("owner-bob", namespace="tenant-a"))

    assert out["memory_id"] == mem_id
    assert out["writer_id"] == "owner-bob"
    assert out["op"] == "create"


@pytest.mark.asyncio
async def test_proof_root_still_crosses_tenants(route, backend):
    """Root bypasses the visibility predicate, as on every other read path."""
    mem_id = f"mem_{uuid.uuid4().hex}"
    await _seed_memory_with_audit_entry(backend, memory_id=mem_id, owner_id="owner-carol", namespace="tenant-a")

    out = await route.audit_proof_head(memory_id_str=mem_id, user=_user("root-user", namespace="tenant-z", role="root"))

    assert out["memory_id"] == mem_id
    assert out["writer_id"] == "owner-carol"


@pytest.mark.asyncio
async def test_proof_unknown_memory_and_invisible_memory_are_indistinguishable(route, backend):
    """Collapse "exists but invisible" and "does not exist" to the same 404."""
    invisible_id = f"mem_{uuid.uuid4().hex}"
    unknown_id = f"mem_{uuid.uuid4().hex}"
    await _seed_memory_with_audit_entry(backend, memory_id=invisible_id, owner_id="owner-dave", namespace="tenant-a")
    attacker = _user("attacker", namespace="tenant-b")

    with pytest.raises(HTTPException) as invisible:
        await route.audit_proof_head(memory_id_str=invisible_id, user=attacker)
    with pytest.raises(HTTPException) as missing:
        await route.audit_proof_head(memory_id_str=unknown_id, user=attacker)

    assert invisible.value.status_code == missing.value.status_code == 404
    # The detail echoes the caller-supplied id and nothing else, so the two
    # responses differ only by the id the caller already provided.
    assert invisible.value.detail == f"no audit entries for memory_id={invisible_id}"
    assert missing.value.detail == f"no audit entries for memory_id={unknown_id}"
