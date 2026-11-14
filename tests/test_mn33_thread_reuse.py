"""MN-33: the edge fixture must not depend on OS thread-id reuse.

``tests/test_edge_profile_unsupported_routes.py``'s ``edge_client`` fixture
used to return a bare ``TestClient`` — no ``with`` — so starlette built a
fresh anyio portal thread for every request. Without ``aiosqlite`` (a dev
venv without the ``sqlite``/``edge`` extra) the SQLite backend holds a raw
``sqlite3`` connection, which may only be used from the thread that opened
it. The bare fixture only stayed green while the OS handed each new portal
thread the previous portal thread's id — and glibc does that often enough
that the coincidence usually holds, which is exactly why the flake was
intermittent rather than obviously broken.

The fixture is now context-managed (``with TestClient(app) as client``), so
its whole lifetime runs on ONE portal thread. This file pins that contract.

Every guarantee here is deterministic. Nothing depends on whether the OS
reuses thread ids: the cross-thread failure is provoked by driving the
connection from a *known different* thread, never by hoping a fresh portal
fails to recycle an id.
"""

from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
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
from mnemos.persistence import sqlite as storage
from mnemos.persistence.sqlite import SqliteBackend


def _user() -> UserContext:
    return UserContext(user_id="u", group_ids=[], role="root", namespace="default", authenticated=True)


def _owning_thread(conn: sqlite3.Connection) -> int:
    """The thread id the raw ``sqlite3`` connection is bound to.

    A probe ``execute`` from the current thread either succeeds (the caller IS
    the owner) or raises with the owning id in the message.

    This helper only ever runs against the raw ``sqlite3`` fallback in these
    tests (the fixture forces ``storage.aiosqlite`` to ``None``, so
    ``SqliteBackend.open`` builds a ``sqlite3`` connection whose ``execute``
    returns a cursor synchronously). The coroutine branch below is purely
    defensive: on the aiosqlite path ``execute`` returns a never-started
    coroutine, and calling ``.close()`` on a never-started coroutine is the
    documented way to dispose of it without running it — it marks the
    coroutine closed so the event loop never reports "coroutine was never
    awaited" and no driver worker stays busy. The helper then reports the
    current thread, which is correct for the multiplexed driver (any thread
    may submit through it).
    """
    import asyncio

    try:
        result = conn.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        message = str(exc)
        marker = "created in thread id "
        assert marker in message, f"unexpected sqlite3 error: {message}"
        return int(message.split(marker, 1)[1].split()[0])
    if asyncio.iscoroutine(result):
        result.close()
    return threading.get_ident()


@pytest.fixture
def edge_backend(monkeypatch, tmp_path):
    """The edge fixture's backend wiring on the no-aiosqlite profile.

    ``storage.aiosqlite`` is read at call time inside ``SqliteBackend.open``,
    so forcing it to ``None`` genuinely selects the raw ``sqlite3`` path — the
    profile where the thread affinity actually bites. With aiosqlite installed
    the driver owns its own worker thread and the flake never appears, which is
    why this only ever failed in a minimal venv.

    Teardown closes the backend only when it is still open AND owned by the
    current (fixture) thread; the portal-thread tests below close on the
    portal thread *before* the portal shuts down, so this is a no-op for
    them. An un-closed connection owned by a dead portal thread cannot be
    released from here at all (``sqlite3.Connection.close`` is itself
    thread-affined), so teardown reports the leak with ``pytest.fail``
    instead of spawning a fresh thread and gambling on thread-id recycling.
    Tests that never open the connection (or run the aiosqlite profile) close
    directly here, exactly as before.
    """
    monkeypatch.setattr(storage, "aiosqlite", None)
    backend = SqliteBackend(tmp_path / "edge.sqlite3", SimpleNamespace())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_pool", None)
    monkeypatch.setattr(lifecycle, "_pool_manager", None)
    yield backend
    if backend._conn is not None and not backend._closed:
        if _owning_thread(backend._conn) == threading.get_ident():
            import asyncio as _asyncio

            _asyncio.run(backend.close())
        else:
            backend._conn = None
            pytest.fail(
                "backend connection still open and owned by another (portal) thread; "
                "close it on the owning thread via client.portal.call(backend.close) "
                "inside the `with TestClient(...)` block"
            )
        assert backend._closed, "the fixture must release its connection on teardown"


@pytest.fixture
def edge_app(edge_backend):
    """The edge fixture's app, wrapped in a per-request thread log."""
    app = FastAPI()
    app.include_router(sessions.router)
    app.include_router(entities.router)
    app.include_router(state.router)
    app.include_router(morpheus.router)
    app.include_router(document_import.router)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_root] = _user

    served: list[int] = []

    @app.middleware("http")
    async def _record(request: Request, call_next):
        served.append(threading.get_ident())
        return await call_next(request)

    app.state.served_threads = served
    return app


def _state_roundtrip(client: TestClient) -> None:
    """The two-request round trip ``test_edge_profile_state_route_uses_sqlite_backend`` runs."""
    resp = client.put("/state/edge-key", json={"value": {"ok": True}})
    assert resp.status_code == 200, resp.text
    resp = client.get("/state/edge-key")
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == '{"ok": true}'


def test_context_managed_client_serves_every_request_on_one_thread(edge_app, edge_backend):
    """``with TestClient(app) as client`` pins ONE portal thread for the client.

    This is the contract the edge fixture now relies on. Every request lands
    on the same thread for the client's whole lifetime, so the raw ``sqlite3``
    connection is always used from its owning thread. Deterministic: it asserts
    sameness of observed thread ids, which holds regardless of OS id recycling.

    All backend state is asserted while the portal is still alive, inside the
    ``with`` block; the close runs on the owning portal thread there too, so
    fixture teardown finds an already-released backend.
    """
    with TestClient(edge_app) as client:
        _state_roundtrip(client)
        assert edge_backend._conn is not None, "the round trip never opened the connection"
        served = list(edge_app.state.served_threads)
        assert len(served) >= 2, f"expected at least two served requests, saw {len(served)}"
        assert len(set(served)) == 1, f"requests ran on several threads: {sorted(set(served))}"
        # Single probe on the portal thread: a clean return (no
        # ProgrammingError) proves this thread owns the raw connection.
        assert client.portal.call(_owning_thread, edge_backend._conn) == client.portal.call(
            threading.get_ident
        )
        client.portal.call(edge_backend.close)
        assert edge_backend._closed, "the round trip must close on the owning portal thread"
        assert edge_backend._conn is None, "the backend must release its connection on close"


def test_context_managed_client_matches_connection_owning_thread(edge_app, edge_backend):
    """The client's single thread is the thread that owns the raw connection.

    The round trip opens and uses the raw connection on the TestClient
    portal thread, and the close runs on that same thread
    (``client.portal.call(backend.close)``) before the portal shuts down —
    no cross-thread close, no thread-id coincidence. ``portal.call`` executes
    on the portal thread itself, so the recorded serving thread must equal
    the portal's own ident, and the backend must be fully released after.

    Everything is asserted while the portal is still alive, inside the
    ``with`` block: after ``close()`` the helper is never called on the
    dead connection, and no backend attribute is read past teardown.
    """
    with TestClient(edge_app) as client:
        _state_roundtrip(client)
        assert edge_backend._conn is not None, "the round trip never opened the connection"
        portal_ident = client.portal.call(threading.get_ident)
        # One probe, on the portal thread, before the close: a clean return
        # proves the portal thread owns the raw connection.
        assert client.portal.call(_owning_thread, edge_backend._conn) == portal_ident
        served = set(edge_app.state.served_threads)
        assert len(served) == 1, f"requests ran on several threads: {sorted(served)}"
        assert served == {portal_ident}, (
            f"client thread {sorted(served)} is not the portal thread {portal_ident}"
        )
        client.portal.call(edge_backend.close)
        assert edge_backend._closed, "the backend must be closed on the owning thread"
        assert edge_backend._conn is None, (
            "the backend must release its connection on the owning thread"
        )


def test_raw_connection_rejects_cross_thread_use_deterministically(tmp_path):
    """The failure mode MN-33 hid: a raw sqlite3 connection is thread-bound.

    Provoked deterministically — the connection is opened on this thread and
    used from a *different, known* thread — so it does not depend on whether
    the OS happens to recycle thread ids. This is the reason a bare
    ``TestClient`` could not be safe without ``aiosqlite``.
    """
    path = str(tmp_path / "mn33-cross-thread.sqlite3")
    conn = sqlite3.connect(path)  # check_same_thread=True by default
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        owner = threading.get_ident()

        seen: list[int] = []
        errors: list[BaseException] = []

        def _use_from_other_thread() -> None:
            seen.append(threading.get_ident())
            try:
                conn.execute("SELECT 1")
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
                errors.append(exc)

        worker = threading.Thread(target=_use_from_other_thread, name="mn33-other-thread")
        worker.start()
        worker.join(timeout=10)

        assert not worker.is_alive(), "the probe thread failed to finish"
        assert seen and seen[0] != owner, "the probe did not run on a different thread"
        assert len(errors) == 1, f"expected one cross-thread error, got {errors!r}"
        assert isinstance(errors[0], sqlite3.ProgrammingError), errors[0]
        assert "only be used in that same thread" in str(errors[0])
    finally:
        conn.close()


def test_aiosqlite_profile_serves_bare_requests(edge_app, edge_backend, monkeypatch):
    """With ``aiosqlite`` installed the connection owns its worker thread.

    The driver multiplexes statements onto that worker, so the client's
    per-request portals are harmless — the flake only exists on the raw
    sqlite3 fallback. Guarded by ``importorskip`` because the minimal venv
    this file targets has no aiosqlite.
    """
    aiosqlite = pytest.importorskip("aiosqlite")
    monkeypatch.setattr(storage, "aiosqlite", aiosqlite)

    client = TestClient(edge_app)
    _state_roundtrip(client)

    served = edge_app.state.served_threads
    assert len(served) >= 2, f"expected at least two served requests, saw {len(served)}"
    assert edge_backend._conn is not None, "the round trip never opened the connection"
    # The driver owns the worker thread, not the request portals: every
    # request may come from a different portal, but the connection itself is
    # served from the driver's own thread and stays open for the client.
    assert edge_backend._closed is False, "the driver must keep the connection open during the round trip"
