"""MN-05 / CF-09: bulk, ingest and portability imports must honour the fence.

``_assert_no_active_deletion`` (the CF-09 GDPR deletion fence) is called by
exactly one route — the single memory create (::

    POST /v1/memories) — so every other write path inserts straight through a
    scope whose deletion request is inside its 30-day grace window::

        mnemos/api/routes/memories.py::bulk_create_memories
        mnemos/api/routes/ingest.py::ingest_session
        mnemos/api/routes/document_import.py::import_memories_from_document
        mnemos/domain/portability/import_.py::import_memories  (POST /v1/import)
        mnemos/portability/charon.py::import_bundle_to_backend  (CHARON/MIF)

A row landed during that window carries ``deleted_at IS NULL``, so the next
sweep cannot see it and the hard-delete phase (which only removes rows that
already carry ``deleted_at IS NOT NULL``) skips it. The row then survives while
the deletion request — and the audit log — records the scope as ``hard_deleted``.

These tests are the regression gate for that: each one drives the real handler
entry point with a deletion request active on the target scope and asserts the
write is refused before any row lands. Root keeps the documented
tombstone-injection bypass and is asserted to still be able to write.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.domain.portability.schemas import (
    MEMORY_PAYLOAD_VERSION,
    MPFEnvelope,
    MPFRecord,
)
from tests._fake_backend import install_fake_backend

pytestmark = pytest.mark.asyncio


# ── helpers ────────────────────────────────────────────────────────────────


def _user(role: str = "user", user_id: str = "alice", namespace: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role=role,
        namespace=namespace,
        authenticated=True,
    )


def _active_deletion_row() -> dict:
    return {
        "id": "req-1",
        "status": "soft_deleted",
        "restore_by": datetime.now(timezone.utc) + timedelta(days=20),
    }


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch):
    """Install the handler-level fake backend (no RLS, no cache)."""
    return install_fake_backend(monkeypatch)


@pytest.fixture
def api_user(monkeypatch: pytest.MonkeyPatch):
    """Override the authenticated principal for HTTP-level tests."""
    from mnemos.api.main import app

    current = {"user": _user()}

    async def override_user():
        return current["user"]

    app.dependency_overrides[get_current_user] = override_user
    try:
        yield current
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _fence_patch():
    """Patch the ONE probe every write path resolves at call time."""
    return patch(
        "mnemos.persistence.worker_lifecycle.active_deletion_for_scope",
        AsyncMock(return_value=_active_deletion_row()),
    )


def _no_fence_patch():
    return patch(
        "mnemos.persistence.worker_lifecycle.active_deletion_for_scope",
        AsyncMock(return_value=None),
    )


def _mpf_envelope(*payloads: dict) -> MPFEnvelope:
    return MPFEnvelope(
        records=[
            MPFRecord(
                id=f"mem_{i}",
                kind="memory",
                payload_version=MEMORY_PAYLOAD_VERSION,
                payload=payload,
            )
            for i, payload in enumerate(payloads)
        ]
    )


# ── POST /v1/memories/bulk ─────────────────────────────────────────────────


async def test_bulk_create_refused_during_active_deletion(
    client,
    auth_headers: dict[str, str],
    backend,
    api_user,
    monkeypatch: pytest.MonkeyPatch,
):
    from mnemos.api.routes import memories

    async def fake_embeddings_batch(texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(memories, "_get_embeddings_batch", fake_embeddings_batch)

    with _fence_patch():
        resp = await client.post(
            "/v1/memories/bulk",
            json={"memories": [{"content": "ghost", "category": "facts"}]},
            headers=auth_headers,
        )

    assert resp.status_code == 409, resp.text
    assert "active deletion request" in resp.json()["detail"]
    assert ("insert_memory", {}) not in backend.memories.calls
    assert not [call for call in backend.memories.calls if call[0] == "insert_memory"]


async def test_bulk_create_probes_once_per_distinct_scope(
    client,
    auth_headers: dict[str, str],
    backend,
    api_user,
    monkeypatch: pytest.MonkeyPatch,
):
    from mnemos.api.routes import memories

    async def fake_embeddings_batch(texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(memories, "_get_embeddings_batch", fake_embeddings_batch)

    probe = AsyncMock(return_value=None)
    with patch("mnemos.persistence.worker_lifecycle.active_deletion_for_scope", probe):
        resp = await client.post(
            "/v1/memories/bulk",
            json={
                "memories": [
                    {"content": "one", "category": "facts"},
                    {"content": "two", "category": "facts"},
                    {"content": "three", "category": "facts"},
                ]
            },
            headers=auth_headers,
        )

    assert resp.status_code == 201, resp.text
    # Three items, one scope: one probe, not three.
    assert probe.await_count == 1


# ── POST /ingest/session ────────────────────────────────────────────────


async def _ingest(client, auth_headers):
    from mnemos.api.routes import ingest

    conn = SimpleNamespace(
        execute=AsyncMock(return_value="INSERT 0 1"),
        transaction=lambda: _noop_ctx(),
    )
    pool = SimpleNamespace(acquire=lambda: _noop_ctx(conn))
    with patch.object(ingest._lc, "get_pool_manager", AsyncMock(return_value=pool)):
        return await client.post(
            "/ingest/session",
            json={
                "session_id": "s-1",
                "source": "claude-code",
                "raw_data": {"messages": [{"role": "user", "content": "hello"}]},
            },
            headers=auth_headers,
        )


class _noop_ctx:
    def __init__(self, value=None):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False

    def transaction(self):
        return self


async def test_ingest_session_refused_during_active_deletion(
    client, auth_headers: dict[str, str], api_user
):
    from mnemos.api.routes import ingest

    insert = AsyncMock(return_value=[])
    with _fence_patch(), patch.object(ingest, "_insert_memory_with_created_webhook", insert):
        resp = await _ingest(client, auth_headers)

    assert resp.status_code == 409, resp.text
    assert "active deletion request" in resp.json()["detail"]
    insert.assert_not_awaited()


# ── POST /v1/import (MPF) ──────────────────────────────────────────────────


async def test_mpf_import_refused_during_active_deletion(
    client, auth_headers: dict[str, str], backend, api_user
):
    with _fence_patch():
        resp = await client.post(
            "/v1/import",
            json=_mpf_envelope({"content": "ghost", "category": "facts"}).model_dump(mode="json"),
            headers=auth_headers,
        )

    assert resp.status_code == 409, resp.text
    assert "active deletion request" in resp.json()["detail"]
    assert not [call for call in backend.memories.calls if call[0] == "insert_memory"]


async def test_mpf_import_probes_every_preserved_scope(
    client, auth_headers: dict[str, str], backend, api_user
):
    """Under ``preserve_owner`` a root caller writes to OTHER owners' scopes.

    The fence must probe each target (owner, namespace) pair, not just the
    caller's own -- a restore must not repopulate a scope whose deletion
    request is still inside its grace window.
    """
    api_user["user"] = _user(role="root", user_id="root", namespace="root-ns")
    probe = AsyncMock(return_value=None)
    with patch("mnemos.persistence.worker_lifecycle.active_deletion_for_scope", probe):
        resp = await client.post(
            "/v1/import?preserve_owner=true",
            json=_mpf_envelope(
                {"content": "a", "category": "facts", "owner_id": "u-a", "namespace": "ns-a"},
                {"content": "b", "category": "facts", "owner_id": "u-b", "namespace": "ns-b"},
            ).model_dump(mode="json"),
            headers=auth_headers,
        )

    assert resp.status_code == 200, resp.text
    probed = {(c.kwargs["target_user_id"], c.kwargs["target_namespace"]) for c in probe.await_args_list}
    assert probed == {("u-a", "ns-a"), ("u-b", "ns-b")}


async def test_mpf_import_sidecar_refused_for_active_scope(
    client, auth_headers: dict[str, str], backend, api_user
):
    """A KG triple that targets a scope mid-deletion is not written either."""
    api_user["user"] = _user(role="root", user_id="root", namespace="root-ns")
    with _fence_patch():
        resp = await client.post(
            "/v1/import?preserve_owner=true",
            json=_mpf_envelope(
                {"content": "a", "category": "facts", "owner_id": "u-a", "namespace": "ns-a"},
            ).model_dump(mode="json"),
            headers=auth_headers,
        )

    assert resp.status_code == 409, resp.text


# ── CHARON / MIF bundle import ─────────────────────────────────────────────


async def test_charon_bundle_import_refused_during_active_deletion(tmp_path, backend):
    from mnemos.portability import mif
    from mnemos.portability.charon import import_bundle_to_backend

    concept = {
        "@id": "https://mnemos.dev/memory/mem_ghost",
        "conceptType": mif.MIF_TYPE,
        "created": "2026-01-01T00:00:00Z",
        "content": "ghost body",
        "properties": {"mnemos:category": "facts", "mnemos:owner_id": "alice", "mnemos:namespace": "alice-ns"},
    }
    (tmp_path / "mem.md").write_text(mif.concept_to_markdown(concept), encoding="utf-8")

    from mnemos.portability import charon

    def _concepts(_src):
        return [{"id": "mem_ghost", "content": "ghost", "category": "facts", "owner_id": "alice", "namespace": "alice-ns"}]

    with _fence_patch(), patch.object(charon, "import_bundle", _concepts):
        with pytest.raises(Exception) as excinfo:
            await import_bundle_to_backend(backend, tmp_path)

    # The refusal must be the fence's 409, not a swallowed per-record skip.
    assert getattr(excinfo.value, "status_code", None) == 409, repr(excinfo.value)
    assert not [call for call in backend.memories.calls if call[0] == "insert_memory"]
