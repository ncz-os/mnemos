from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user, require_root

from mnemos.api.routes import (
    document_import,
    entities,
    morpheus,
    sessions,
    state,
)
from mnemos.persistence.base import SESSIONS_CAPABILITY, STATE_CAPABILITY
from mnemos.persistence.sqlite import SqliteBackend


# Every profile below provisions its OWN SQLite file. Sharing one file under
# one directory (``tmp_path / "edge.sqlite3"``) is what let the session and
# state fixtures lean on the row a previous profile had left behind: they read
# a persisted row instead of exercising the contract under the profile they
# claimed to test. Per-profile files are the unit under test, so thread-id and
# state-row reuse between profiles is not reachable from here any more.
PROFILE_STORAGE = {
    "raw-route-sqlite": "raw-routes.sqlite3",
    "raw-session-sqlite": "raw-sessions.sqlite3",
    "raw-state-sqlite": "raw-state.sqlite3",
}
# What ``SessionsRepository.create_session`` returns on every backend (the
# Postgres implementation selects exactly these three columns).
_SESSION_ROW_KEYS = {"id", "created_at", "model"}


def _user() -> UserContext:
    return UserContext(user_id="u", group_ids=[], role="root", namespace="default", authenticated=True)


def _backend(monkeypatch, tmp_path, profile):
    """Provision the SQLite backend for exactly ONE edge profile."""
    backend = SqliteBackend(tmp_path / PROFILE_STORAGE[profile], SimpleNamespace())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_pool", None)
    monkeypatch.setattr(lifecycle, "_pool_manager", None)
    return backend


def _route_client():
    app = FastAPI()
    app.include_router(sessions.router)
    app.include_router(entities.router)
    app.include_router(state.router)
    app.include_router(morpheus.router)
    app.include_router(document_import.router)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_root] = _user
    return TestClient(app)


@pytest.fixture
def edge_client(monkeypatch, tmp_path):
    _backend(monkeypatch, tmp_path, "raw-route-sqlite")
    return _route_client()


def test_edge_profile_postgres_only_routes_return_503(edge_client):
    cases = [
        ("get", "/entities", {}),
        ("get", "/v1/morpheus/runs", {}),
    ]
    for method, path, kwargs in cases:
        resp = getattr(edge_client, method)(path, **kwargs)
        assert resp.status_code == 503
        # Detail wording differs across routes — all hit
        # ``_require_postgres_backend`` which produces the
        # "requires a Postgres backend" message.
        assert "Postgres backend" in resp.text


def test_edge_profile_state_route_uses_sqlite_backend(edge_client):
    resp = edge_client.put("/state/edge-key", json={"value": {"ok": True}})
    assert resp.status_code == 200, resp.text

    resp = edge_client.get("/state/edge-key")
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == '{"ok": true}'


def test_edge_profile_documents_import_no_longer_requires_postgres(edge_client, monkeypatch):
    """Document import is backend-lifted; SQLite profiles should no
    longer fail at the Postgres-only route gate."""
    monkeypatch.setattr(document_import, "DOCLING_AVAILABLE", False)
    resp = edge_client.post(
        "/v1/documents/import",
        files={"file": ("doc.txt", b"hello", "text/plain")},
        data={"project_tag": "mnemos"},
    )
    assert resp.status_code == 501
    assert "Postgres backend" not in resp.text


def test_edge_profile_documents_batch_import_no_longer_requires_postgres(edge_client, monkeypatch):
    monkeypatch.setattr(document_import, "DOCLING_AVAILABLE", False)
    resp = edge_client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("a.txt", b"hello", "text/plain")),
            ("files", ("b.txt", b"world", "text/plain")),
        ],
        data={"project_tag": "mnemos"},
    )
    assert resp.status_code == 207
    assert "Postgres backend" not in resp.text
    assert {entry["status_code"] for entry in resp.json()} == {501}


def test_edge_profiles_do_not_reuse_each_others_storage(monkeypatch, tmp_path):
    """Two edge profiles must not be able to reach each other's rows.

    The fixture used to open one shared SQLite file for every test in this
    module, so a row created by one test was visible to the next. Path
    inequality alone does not prove that: this writes a session through one
    profile's store and reads it back through the other's, the way the shared
    fixture did.

    The write direction is an explicit argument (review finding F2, round 2):
    ``first -> second`` alone would only catch a leak that flowed one way.
    Both directions are asserted -- write in ``first`` read through
    ``second``, and write in ``second`` read through ``first`` -- plus each
    profile's own round-trip, so the isolation contract holds regardless of
    which profile writes first.
    """
    asyncio = pytest.importorskip("asyncio")

    first = _backend(monkeypatch, tmp_path, "raw-session-sqlite")
    second = _backend(monkeypatch, tmp_path, "raw-state-sqlite")
    assert first is not second
    assert first._db_path != second._db_path

    async def _create_then_read_through(writer: SqliteBackend, reader: SqliteBackend):
        """Write a session through ``writer``; read it back through ``reader``."""
        async with writer.transactional() as tx:
            row = await writer.sessions.create_session(
                tx, user_id="u", namespace="default", model="m", initial_context=None
            )
        async with reader.transactional() as tx:
            return await reader.sessions.get_session(tx, row["id"], "u", "default")

    # Same-profile round-trips must succeed...
    assert asyncio.run(_create_then_read_through(first, first)) is not None
    assert asyncio.run(_create_then_read_through(second, second)) is not None
    # ...and neither profile may see the other's row, in either direction.
    assert asyncio.run(_create_then_read_through(first, second)) is None
    assert asyncio.run(_create_then_read_through(second, first)) is None


def test_sessions_repository_create_session_on_raw_sqlite(monkeypatch, tmp_path):
    """``SessionsRepository.create_session`` on the raw SQLite profile must
    behave like every other backend: it returns the row it just created, with
    a unique session id and the chat-session columns.

    Postgres derives that row from ``INSERT ... RETURNING id``; the SQLite
    implementation hard-codes a ``sessions`` projection, so a drift between
    the two surfaces as a None/partial row here instead of as a 500 in the
    route.
    """
    asyncio = pytest.importorskip("asyncio")

    backend = _backend(monkeypatch, tmp_path, "raw-session-sqlite")
    other_store = SqliteBackend(tmp_path / "raw-session-sqlite-copy.sqlite3", SimpleNamespace())

    async def create(store):
        async with store.transactional() as tx:
            return await store.sessions.create_session(
                tx, user_id="u", namespace="default", model="m", initial_context=None
            )

    async def read(store, session_id, namespace="default"):
        async with store.transactional() as tx:
            return await store.sessions.get_session(tx, session_id, "u", namespace)

    first = asyncio.run(create(backend))
    second = asyncio.run(create(backend))

    assert first is not None and second is not None
    assert first["id"] and second["id"]
    assert first["id"] != second["id"], "two sessions must not share one id"
    assert set(first.keys()) == _SESSION_ROW_KEYS

    assert asyncio.run(read(backend, first["id"]))["id"] == first["id"]

    # The row belongs to one namespace and one store, not to the process.
    assert asyncio.run(read(backend, first["id"], namespace="elsewhere")) is None
    assert asyncio.run(read(other_store, first["id"])) is None


def test_edge_profile_sessions_route_does_not_require_postgres(edge_client):
    """The sessions surface is backend-lifted; a raw-SQLite profile must not
    answer it with the Postgres-only 503."""
    resp = edge_client.post("/v1/sessions", json={"model": "m"})
    assert resp.status_code != 503, resp.text
    assert resp.status_code == 200, resp.text

    session_id = resp.json()["session_id"]
    fetched = edge_client.get(f"/v1/sessions/{session_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["session_id"] == session_id


def test_edge_profile_sessions_route_requires_the_sessions_capability(monkeypatch, tmp_path):
    """A backend that does not advertise sessions answers 503 — it must not
    fall through to reading another profile's persisted row."""

    backend = _backend(monkeypatch, tmp_path, "raw-route-sqlite")

    class _StateOnlyBackend:
        capabilities = {STATE_CAPABILITY}
        sessions = backend.sessions

        @asynccontextmanager
        async def transactional(self):
            async with backend.transactional() as tx:
                yield tx

    assert SESSIONS_CAPABILITY not in _StateOnlyBackend.capabilities
    monkeypatch.setattr(lifecycle, "_persistence_backend", _StateOnlyBackend())

    resp = _route_client().post("/v1/sessions", json={"model": "m"})
    assert resp.status_code == 503, resp.text
    assert "sessions" in resp.text.lower()
