from __future__ import annotations

import asyncio
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
    "raw-session-sqlite-copy": "raw-session-copy.sqlite3",
}
# What the route must be able to read back from
# ``SessionsRepository.create_session`` on every backend. The SQLite and
# Postgres implementations both return at least ``id``, ``created_at`` and
# ``model`` (Postgres selects exactly these three columns via
# ``INSERT ... RETURNING id``); the ABC contract itself requires ``id``,
# ``user_id``, ``namespace`` and ``model``. The repository test pins only the
# contract subset (round 1/2, F3); ``created_at`` is pinned at the route
# round-trip, where ``SessionResponse`` actually needs it.
_SESSION_ROW_KEYS = {"id", "created_at", "model"}

# The lifecycle slots every provisioned store registers through.
_REGISTER_SLOTS = ("_persistence_backend", "_pool", "_pool_manager")


def _point_to_lifecycle(backend) -> None:
    """Point the module slots at ``backend`` (caller owns teardown).

    Used only inside the ``lifecycle_registration`` fixture, whose
    ``monkeypatch``-based teardown restores every slot automatically —
    even when the test body raises.
    """
    for name in _REGISTER_SLOTS:
        setattr(lifecycle, name, backend if name == "_persistence_backend" else None)


@pytest.fixture
def lifecycle_registration(monkeypatch):
    """Manual lifecycle-registration with pytest-guaranteed teardown.

    Captures the current slot values into ``monkeypatch`` on entry (once,
    so registering a second store in sequence does not record the first
    store as "original") and yields :func:`_point_to_lifecycle`.
    ``monkeypatch`` restores the real values on teardown — no manual
    restore call, no module-global snapshot, no cross-test leakage if the
    test fails or is interrupted.
    """
    monkeypatch.setattr(lifecycle, "_persistence_backend", lifecycle._persistence_backend)
    monkeypatch.setattr(lifecycle, "_pool", lifecycle._pool)
    monkeypatch.setattr(lifecycle, "_pool_manager", lifecycle._pool_manager)
    yield _point_to_lifecycle


def _user() -> UserContext:
    return UserContext(user_id="u", group_ids=[], role="root", namespace="default", authenticated=True)


def _backend(monkeypatch, tmp_path, profile):
    """Provision the SQLite backend for exactly ONE edge profile.

    Registration goes through monkeypatch so pytest restores the lifecycle
    state automatically on teardown; the caller only closes the backend.
    """
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
    # MN-33: the client is context-managed so its whole lifetime runs on ONE
    # portal thread. A bare ``TestClient(app)`` spins a fresh anyio portal
    # per request; without aiosqlite the SQLite backend holds a raw
    # ``sqlite3`` connection bound to the thread that opened it, so the
    # round-trip only stayed green while the OS recycled the previous
    # portal's thread id. That is an order-dependent flake, not a contract.
    backend = _backend(monkeypatch, tmp_path, "raw-route-sqlite")
    with _route_client() as client:
        yield client
    # Release the provisioned file handle before the next profile opens.
    asyncio.run(backend.close())


def test_edge_client_is_context_managed_single_portal(edge_client):
    # MN-33 regression guard: the fixture must yield a client entered via
    # ``with TestClient(app)``, which provisions ONE anyio portal (and hence
    # one portal thread) for its whole lifetime. A bare ``TestClient(app)``
    # has ``portal is None`` until entered and then spins a fresh portal per
    # request — which breaks the raw ``sqlite3`` connection's thread
    # affinity in order-dependent ways.
    assert edge_client.portal is not None


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

    Each profile provisions its own SQLite file via PROFILE_STORAGE, so
    path inequality is enforced by construction. This test verifies the
    isolation contract explicitly by writing through one profile's store and
    reading through the other's, in both directions, plus each profile's own
    round-trip.

    The write direction is an explicit argument (review finding F2, round 2):
    ``first -> second`` alone would only catch a leak that flowed one way.
    Both directions are asserted -- write in ``first`` read through
    ``second``, and write in ``second`` read through ``first`` -- plus each
    profile's own round-trip, so the isolation contract holds regardless of
    which profile writes first.
    """
    first = _backend(monkeypatch, tmp_path, "raw-session-sqlite")
    second = _backend(monkeypatch, tmp_path, "raw-state-sqlite")
    try:
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
    finally:
        # Release both file handles before the next profile provisions.
        asyncio.run(first.close())
        asyncio.run(second.close())


def test_sessions_repository_create_session_on_raw_sqlite(tmp_path, lifecycle_registration):
    """``SessionsRepository.create_session`` on the raw SQLite profile must
    behave like every other backend: it returns the row it just created, with
    a unique session id and the chat-session columns.

    Postgres derives that row from ``INSERT ... RETURNING id``; the SQLite
    implementation hard-codes a ``sessions`` projection, so a drift between
    the two surfaces as a None/partial row here instead of as a 500 in the
    route.

    The lifecycle slots are managed by the ``lifecycle_registration``
    fixture (pytest teardown, not a module-global snapshot): the primary
    store is registered for the duration of the test, and the "no reuse
    across stores" check provisions a second store directly — without ever
    leaving the module slots pointing at a store about to be closed.
    """
    backend = SqliteBackend(tmp_path / PROFILE_STORAGE["raw-session-sqlite"], SimpleNamespace())
    lifecycle_registration(backend)
    # The "no reuse across stores" check needs a second, equally provisioned
    # store. It is exercised directly (its own ``transactional()`` context),
    # never via the module slots — so there is no intermediate registration
    # and no need to "flip" the live slots back.
    other_store = SqliteBackend(tmp_path / PROFILE_STORAGE["raw-session-sqlite-copy"], SimpleNamespace())

    try:
        async def main():
            async def create(store):
                async with store.transactional() as tx:
                    return await store.sessions.create_session(
                        tx, user_id="u", namespace="default", model="m", initial_context=None
                    )

            async def read(store, session_id, namespace="default"):
                async with store.transactional() as tx:
                    return await store.sessions.get_session(tx, session_id, "u", namespace)

            first = await create(backend)
            second = await create(backend)

            assert first is not None and second is not None
            assert first["id"] and second["id"]
            assert first["id"] != second["id"], "two sessions must not share one id"
            # Contract, not projection drift (review round 1/2, finding F3):
            # the load-bearing check is that the ABC contract columns are
            # present. The previous ``set(first.keys()) == _SESSION_ROW_KEYS``
            # upper bound forced this SQLite projection to match exactly and
            # failed on any legitimate addition — even though ``created_at``
            # is not part of the ``SessionsRepository.create_session``
            # contract (only ``id``, ``user_id``, ``namespace`` and ``model``
            # are required by the ABC). Keep the lower bound only; the route
            # round-trip test pins the response fields separately, where
            # they are actually required.
            assert _SESSION_ROW_KEYS <= set(first.keys())

            assert (await read(backend, first["id"]))["id"] == first["id"]

            # The row belongs to one namespace and one store, not to the
            # process.
            assert await read(backend, first["id"], namespace="elsewhere") is None
            assert await read(other_store, first["id"]) is None

        asyncio.run(main())
    finally:
        # Release both file handles; the lifecycle slots are restored by the
        # fixture teardown, never by this test.
        asyncio.run(backend.close())
        asyncio.run(other_store.close())


def test_edge_profile_sessions_route_does_not_require_postgres(edge_client):
    """The sessions surface is backend-lifted; a raw-SQLite profile must not
    answer it with the Postgres-only 503."""
    resp = edge_client.post("/v1/sessions", json={"model": "m"})
    assert resp.status_code != 503, resp.text
    assert resp.status_code == 200, resp.text

    body = resp.json()
    session_id = body["session_id"]
    # The response model carries the row contract end-to-end, not just an id:
    # drift in the SQLite create_session projection must surface here, not be
    # masked by a client that reads only the identifier. These pins cover the
    # fields the repository row must provide (``model`` and ``created_at`` in
    # the ``SessionResponse``) — the contract columns that are no longer
    # pinned by the repository test's exact-key upper bound (round 1/2, F3).
    assert body["model"] == "m"
    assert body["created_at"], "the created_at field must be present and non-empty"

    fetched = edge_client.get(f"/v1/sessions/{session_id}")
    assert fetched.status_code == 200, fetched.text
    fetched_body = fetched.json()
    assert fetched_body["session_id"] == session_id
    # The round trip returns the context object, not a bare identifier: the
    # session belongs to this namespace/user and carries the model we created it with.
    assert fetched_body["model"] == "m"


def test_edge_profile_sessions_route_requires_the_sessions_capability(monkeypatch, tmp_path):
    """A backend that does not advertise sessions answers 503 — it must not
    fall through to reading another profile's persisted row."""

    backend = _backend(monkeypatch, tmp_path, "raw-route-sqlite")

    class _StateOnlyBackend:
        """A real ``SqliteBackend`` facade with ONLY the state capability
        advertised.

        It shares the provisioned SQLite store (so ``transactional()`` is the
        real backend context manager, not a hand-rolled double) but hides
        every other feature-layer flag via a narrowed ``capabilities`` view.
        Because the real backend still carries the ``sessions`` repository,
        this isolates the capability-gate path in
        ``require_sessions_backend``: with ``SESSIONS_CAPABILITY`` missing from
        the advertised set, the route must answer 503 before it ever touches
        the store — never falling through to read a row persisted by another
        profile.
        """

        def __init__(self, real_backend):
            self._real = real_backend

        def __getattr__(self, name):
            if name == "sessions":
                raise AssertionError(
                    "capability gate bypassed: the route touched 'sessions' "
                    "on a backend that does not advertise SESSIONS_CAPABILITY"
                )
            return getattr(self._real, name)

        @property
        def capabilities(self):
            return {STATE_CAPABILITY}

    assert SESSIONS_CAPABILITY not in _StateOnlyBackend(backend).capabilities
    monkeypatch.setattr(lifecycle, "_persistence_backend", _StateOnlyBackend(backend))

    resp = None
    with _route_client() as _client:
        resp = _client.post("/v1/sessions", json={"model": "m"})
    assert resp.status_code == 503, resp.text
    assert "sessions" in resp.text.lower()
