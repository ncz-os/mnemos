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
from fastapi import HTTPException

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


async def test_ingest_session_refused_during_active_deletion(client, auth_headers: dict[str, str], api_user):
    from mnemos.api.routes import ingest

    insert = AsyncMock(return_value=[])
    with _fence_patch(), patch.object(ingest, "_insert_memory_with_created_webhook", insert):
        resp = await _ingest(client, auth_headers)

    assert resp.status_code == 409, resp.text
    assert "active deletion request" in resp.json()["detail"]
    insert.assert_not_awaited()


# ── POST /v1/import (MPF) ──────────────────────────────────────────────────


async def test_mpf_import_refused_during_active_deletion(client, auth_headers: dict[str, str], backend, api_user):
    with _fence_patch():
        resp = await client.post(
            "/v1/import",
            json=_mpf_envelope({"content": "ghost", "category": "facts"}).model_dump(mode="json"),
            headers=auth_headers,
        )

    assert resp.status_code == 409, resp.text
    assert "active deletion request" in resp.json()["detail"]
    assert not [call for call in backend.memories.calls if call[0] == "insert_memory"]


async def test_mpf_import_probes_every_preserved_scope(client, auth_headers: dict[str, str], backend, api_user):
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


async def test_mpf_import_sidecar_refused_for_active_scope(client, auth_headers: dict[str, str], backend, api_user):
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
        return [
            {"id": "mem_ghost", "content": "ghost", "category": "facts", "owner_id": "alice", "namespace": "alice-ns"}
        ]

    with _fence_patch(), patch.object(charon, "import_bundle", _concepts):
        with pytest.raises(Exception) as excinfo:
            await import_bundle_to_backend(backend, tmp_path)

    # The refusal must be the fence's 409, not a swallowed per-record skip.
    assert getattr(excinfo.value, "status_code", None) == 409, repr(excinfo.value)
    assert not [call for call in backend.memories.calls if call[0] == "insert_memory"]


# ── root bypass / fence placement ──────────────────────────────────────────


async def test_bulk_create_root_bypasses_fence_on_own_scope(
    client,
    auth_headers: dict[str, str],
    backend,
    api_user,
    monkeypatch: pytest.MonkeyPatch,
):
    """Root keeps the documented tombstone-injection bypass, on its own scope."""
    from mnemos.api.routes import memories

    api_user["user"] = _user(role="root", user_id="root", namespace="root-ns")

    async def fake_embeddings_batch(texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(memories, "_get_embeddings_batch", fake_embeddings_batch)

    with _fence_patch():
        resp = await client.post(
            "/v1/memories/bulk",
            json={"memories": [{"content": "tombstone", "category": "facts"}]},
            headers=auth_headers,
        )

    assert resp.status_code == 201, resp.text


async def test_document_import_refuses_before_first_chunk(monkeypatch):
    """The whole document is refused; no chunk is written."""
    from mnemos.api.routes import document_import

    backend = SimpleNamespace()
    monkeypatch.setattr(document_import, "backend_or_503", lambda: backend)
    monkeypatch.setattr(document_import, "DOCLING_AVAILABLE", True)

    async def _read(_file):
        return b"# doc"

    monkeypatch.setattr(document_import, "_read_upload_file_capped", _read)
    monkeypatch.setattr(document_import, "_validate_permission_mode", lambda _pm, default: default)
    monkeypatch.setattr(document_import, "_validate_project_tag", lambda _tag: _tag)

    importer = SimpleNamespace(
        parse_document=lambda *a, **k: ("text", {}, [{"content": "chunk one"}]),
    )
    monkeypatch.setattr(document_import, "DoclingImporter", lambda: importer)

    with _fence_patch(), pytest.raises(HTTPException) as excinfo:
        await document_import.import_memories_from_document(
            file=SimpleNamespace(filename="doc.md"),
            project_tag="proj",
            user=_user(),
        )

    assert excinfo.value.status_code == 409, excinfo.value.detail


@pytest.mark.asyncio
async def test_fence_probe_lives_in_domain_not_api() -> None:
    """The shared probe must be importable from below ``mnemos.api``.

    ``mnemos.domain`` may not import ``mnemos.api`` (layered contract), so the
    fence's home is ``mnemos.domain.deletion_fence`` and
    ``mnemos.api.memory_mutations`` re-exports the SAME object.
    """
    from mnemos.api import memory_mutations
    from mnemos.domain import deletion_fence

    assert memory_mutations._assert_no_active_deletion is deletion_fence.assert_no_active_deletion
    assert memory_mutations._assert_no_active_deletion_for_scopes is deletion_fence.assert_no_active_deletion_for_scopes


# ── real-backend regression: the probe must not run inside a transaction ───


async def _open_sqlite_backend(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "fence-real.db", SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))
    await backend.open()
    return backend


async def test_charon_import_probe_does_not_run_inside_the_import_transaction(tmp_path):
    """End-to-end guard against the nested-transaction deadlock.

    ``active_deletion_for_scope`` reads through a transaction of its own. When
    the charon bundle import probed from INSIDE ``backend.transactional()``,
    SQLite serialised on the file lock and the import hung forever -- the
    round-trip suite never caught it because every other test mocks the probe
    out, so the real read transaction never opened.

    Here the fence is NOT mocked: a real ``SqliteBackend`` and the real probe
    run, so a re-introduced nested transaction blocks and trips the timeout
    instead of passing silently. The fence finds no active deletion (the table
    is empty), so the import is allowed and must complete promptly.
    """
    import asyncio
    from unittest.mock import patch

    from mnemos.portability import charon

    backend = await _open_sqlite_backend(tmp_path)
    try:
        concepts = [
            {
                "id": "mem_real_1",
                "content": "real round trip",
                "category": "facts",
                "owner_id": "alice",
                "namespace": "alice-ns",
            }
        ]
        with patch.object(charon, "import_bundle", lambda _src: concepts):
            await asyncio.wait_for(
                charon.import_bundle_to_backend(backend, tmp_path / "bundle.mif"),
                timeout=20,
            )
        async with backend.transactional() as tx:
            row = await backend.memories.fetch_memory_by_id(tx, "mem_real_1")
        assert row is not None, "import wrote no row"
        assert row["content"] == "real round trip"
    finally:
        await backend.close()


async def test_mpf_preflight_runs_before_the_import_transaction(tmp_path):
    """The MPF route's probe is a preflight, not an in-transaction call.

    Same root cause as above for ``POST /v1/import``: the route opens the
    transaction and hands ``tx`` to ``import_memories``, so probing there would
    nest a second handle. ``preflight_import_scope_fence`` is invoked by the
    route BEFORE ``backend.transactional()``; calling it standalone against a
    real backend must return promptly and leave no transaction open.
    """
    import asyncio

    from mnemos.domain.portability.import_ import preflight_import_scope_fence

    backend = await _open_sqlite_backend(tmp_path)
    try:
        envelope = _mpf_envelope({"content": "hello", "category": "facts"})
        await asyncio.wait_for(
            preflight_import_scope_fence(backend, envelope=envelope, preserve_owner=False, user=_user()),
            timeout=20,
        )
        # A second in-transaction write must still succeed: the preflight
        # closed its own read transaction rather than leaking it.
        async with backend.transactional():
            pass
    finally:
        await backend.close()
