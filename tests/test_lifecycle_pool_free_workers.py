"""Regression coverage for lifespan workers on non-PostgreSQL backends.

The lifespan worker loop skips any registered worker that is not in
``lifecycle._POOL_FREE_LIFESPAN_WORKERS`` when ``_pool is None`` -- which is
every non-PostgreSQL backend (SQLite, Oracle, Db2, MySQL). "federation sync
worker" was registered but missing from that set, so on the SQLite/edge
profile it was dropped without a log line: federation peers only synced via an
explicit ``POST /v1/federation/peers/{id}/sync``, never on their configured
``sync_interval_secs``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _registered_worker_names(monkeypatch) -> list[str]:
    """Names passed to register_lifespan_worker by the API hook module."""
    from mnemos.api import lifecycle_hooks
    from mnemos.core import lifecycle

    captured: list[str] = []

    def _capture(name, factory, *, honor_worker_enabled=False):
        captured.append(name)

    monkeypatch.setattr(lifecycle, "register_lifespan_worker", _capture)
    monkeypatch.setattr(lifecycle, "register_auth_configurer", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "register_provider_manifest_reloader", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "register_lifespan_cleanup_hook", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "register_post_db_startup_hook", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle_hooks, "_registered", False)

    lifecycle_hooks.register_lifespan_hooks()
    return captured


def test_federation_sync_worker_is_allowed_without_a_postgres_pool():
    from mnemos.core import lifecycle

    assert "federation sync worker" in lifecycle._POOL_FREE_LIFESPAN_WORKERS


@pytest.mark.parametrize(
    "worker_name",
    sorted(
        {
            "deletion_request_worker",
            "hard_deletion_request_worker",
            "persephone archival worker",
            "federation sync worker",
            "embedding worker",
        }
    ),
)
def test_pool_free_worker_names_match_their_registration(worker_name, monkeypatch):
    """The allow-list keys off exact name strings, so a rename silently
    re-breaks the worker. Pin each entry to a real registration."""
    assert worker_name in _registered_worker_names(monkeypatch)


def test_federation_sync_worker_factory_needs_no_pool(monkeypatch):
    """Membership in the allow-list is only safe if the factory really is
    pool-free. Call it the way the lifespan loop does on SQLite -- pool
    argument None -- and assert it hands back a live coroutine instead of
    raising or reaching for asyncpg."""
    from mnemos.api import lifecycle_hooks
    from mnemos.core import lifecycle

    monkeypatch.setattr(lifecycle_hooks, "service_enabled", lambda _settings, _name: True)
    monkeypatch.setattr(lifecycle, "_persistence_backend", SimpleNamespace(federation=object()))

    called_with: list[object] = []

    async def _fake_loop(backend):
        called_with.append(backend)

    monkeypatch.setattr("mnemos.domain.federation.federation_worker_loop", _fake_loop)

    coro = lifecycle_hooks._federation_sync_worker(None)

    assert coro is not None, "federation sync worker refused to start without a pool"
    asyncio.run(coro)
    assert called_with == [lifecycle._persistence_backend]


def test_federation_sync_worker_still_honors_profile_opt_out(monkeypatch):
    """Adding the worker to the allow-list must not turn it on for operators
    who never opted in -- the edge profile leaves it off until
    MNEMOS_FEDERATION_ENABLED is set."""
    from mnemos.api import lifecycle_hooks

    monkeypatch.setattr(lifecycle_hooks, "service_enabled", lambda _settings, _name: False)

    assert lifecycle_hooks._federation_sync_worker(None) is None
