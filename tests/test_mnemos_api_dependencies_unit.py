from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from mnemos.api import dependencies
from mnemos.core import config, lifecycle, replica_mode
from mnemos.core.auth_context import UserContext
from mnemos.core.oauth import SESSION_COOKIE_NAME


@pytest.fixture
def auth(monkeypatch):
    settings = SimpleNamespace(
        enabled=True,
        default_namespace="settings-ns",
        personal_user_id="settings-user",
        cache_ttl_seconds=30,
        touch_interval_seconds=60,
    )
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(auth=settings))
    for name in (
        "_auth_enabled",
        "_default_namespace",
        "_personal_user_id",
        "PERSONAL_SINGLETON",
        "_cache_ttl",
        "_touch_interval",
        "_touch_task",
        "_lookups_since_sweep",
        "_touches_since_sweep",
    ):
        monkeypatch.setattr(dependencies, name, getattr(dependencies, name))
    monkeypatch.setattr(dependencies, "_key_cache", {})
    monkeypatch.setattr(dependencies, "_touch_times", {})
    monkeypatch.setattr(dependencies, "_dirty_keys", dependencies.collections.OrderedDict())
    monkeypatch.setattr(replica_mode, "replica_mode_enabled", lambda: False)
    dependencies.configure_auth()
    return settings


@pytest.fixture
def backend():
    tx = object()

    @asynccontextmanager
    async def transaction():
        yield tx

    oauth = SimpleNamespace(
        lookup_api_key=AsyncMock(),
        resolve_active_session=AsyncMock(),
        touch_api_key=AsyncMock(),
    )
    return SimpleNamespace(_supports_oauth_persistence=True, oauth=oauth, transactional=transaction, tx=tx)


def request(backend=None, method="GET", path="/data", cookies=None):
    state = SimpleNamespace(persistence_backend=backend)
    return SimpleNamespace(
        app=SimpleNamespace(state=state), method=method, url=SimpleNamespace(path=path), cookies=cookies or {}
    )


def credential(value="test-key"):
    return SimpleNamespace(credentials=value)


def key_row(key_id="key-1", role="user"):
    return {"id": key_id, "user_id": "alice", "group_ids": ["team"], "role": role, "namespace": "tenant"}


def user(role):
    return UserContext(user_id="alice", group_ids=[], role=role, namespace="tenant", authenticated=True)


def test_configure_auth_defaults_overrides_and_immutable_stats(auth):
    assert dependencies.PERSONAL_SINGLETON == UserContext("settings-user", [], "root", "settings-ns", False)
    assert dependencies.auth_cache_stats() == dependencies.AuthCacheStats(0, 0)
    with pytest.raises(FrozenInstanceError):
        dependencies.auth_cache_stats().key_cache_entries = 1

    dependencies.configure_auth(
        {
            "enabled": False,
            "default_namespace": "local",
            "personal_user_id": "owner",
            "cache_ttl_seconds": "5",
            "touch_interval_seconds": "0",
        }
    )
    assert dependencies.PERSONAL_SINGLETON == UserContext("owner", [], "root", "local", False)


@pytest.mark.asyncio
async def test_disabled_auth_and_unconfigured_startup(auth, monkeypatch):
    dependencies.configure_auth({"enabled": False})
    result = await dependencies.get_current_user(request(), None)
    assert result is dependencies.PERSONAL_SINGLETON
    assert not result.authenticated
    monkeypatch.setattr(dependencies, "PERSONAL_SINGLETON", None)
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(request(), None)
    assert (exc.value.status_code, exc.value.detail) == (503, "Auth not yet configured — startup incomplete")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend_value, detail",
    [
        (None, "Persistence backend not available"),
        (
            SimpleNamespace(_supports_oauth_persistence=False),
            "Authentication requires an OAuth-capable persistence backend",
        ),
    ],
)
async def test_enabled_auth_requires_oauth_backend(auth, backend_value, detail):
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(request(backend_value), None)
    assert exc.value.status_code == 503
    assert detail in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, {"revoked": True}])
async def test_invalid_bearer_is_rejected_without_cookie_fallback(auth, backend, row):
    backend.oauth.lookup_api_key.return_value = row
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(request(backend, cookies={SESSION_COOKIE_NAME: "session"}), credential())
    assert (exc.value.status_code, exc.value.detail) == (401, "Invalid or revoked API key")
    backend.oauth.resolve_active_session.assert_not_awaited()
    assert dependencies.auth_cache_stats().key_cache_entries == 0


@pytest.mark.asyncio
async def test_bearer_cache_expiry_copy_and_reconfiguration(auth, backend, monkeypatch):
    clock = SimpleNamespace(monotonic=lambda: 100.0)
    monkeypatch.setattr(dependencies, "time", clock)
    dependencies.configure_auth({"cache_ttl_seconds": "10", "touch_interval_seconds": "0"})
    backend.oauth.lookup_api_key.return_value = key_row()
    req = request(backend)
    first = await dependencies.get_current_user(req, credential())
    assert first == UserContext("alice", ["team"], "user", "tenant", True, api_key_id="key-1")
    backend.oauth.lookup_api_key.assert_awaited_once_with(backend.tx, hashlib.sha256(b"test-key").hexdigest())
    first.group_ids.append("injected")
    second = await dependencies.get_current_user(req, credential())
    assert second.group_ids == ["team"]
    assert second is not first
    assert backend.oauth.lookup_api_key.await_count == 1
    assert dependencies.auth_cache_stats().key_cache_entries == 1

    clock.monotonic = lambda: 110.0
    await dependencies.get_current_user(req, credential())
    assert backend.oauth.lookup_api_key.await_count == 2
    dependencies.configure_auth({"touch_interval_seconds": 0})
    assert dependencies.auth_cache_stats().key_cache_entries == 0
    await dependencies.get_current_user(req, credential())
    assert backend.oauth.lookup_api_key.await_count == 3


@pytest.mark.asyncio
async def test_cache_invalidation_by_id_hash_and_all(auth, backend):
    dependencies.configure_auth({"touch_interval_seconds": 0})
    backend.oauth.lookup_api_key.side_effect = [key_row("one"), key_row("two"), key_row("one"), key_row("two")]
    req = request(backend)
    await dependencies.get_current_user(req, credential("one"))
    await dependencies.get_current_user(req, credential("two"))
    dependencies.invalidate_api_key_cache(key_id="one")
    assert dependencies.auth_cache_stats().key_cache_entries == 1
    await dependencies.get_current_user(req, credential("two"))
    await dependencies.get_current_user(req, credential("one"))
    assert backend.oauth.lookup_api_key.await_count == 3
    dependencies.invalidate_api_key_cache(key_hash=hashlib.sha256(b"two").hexdigest())
    await dependencies.get_current_user(req, credential("two"))
    assert backend.oauth.lookup_api_key.await_count == 4
    dependencies.invalidate_api_key_cache()
    assert dependencies.auth_cache_stats().key_cache_entries == 0


@pytest.mark.asyncio
async def test_disabled_cache_looks_up_every_request(auth, backend):
    dependencies.configure_auth({"cache_ttl_seconds": 0, "touch_interval_seconds": 0})
    backend.oauth.lookup_api_key.return_value = key_row()
    await dependencies.get_current_user(request(backend), credential())
    await dependencies.get_current_user(request(backend), credential())
    assert backend.oauth.lookup_api_key.await_count == 2
    assert dependencies.auth_cache_stats().key_cache_entries == 0


@pytest.mark.asyncio
async def test_session_resolution_and_missing_credentials(auth, backend):
    backend.oauth.resolve_active_session.return_value = {"user_id": "session-user"}
    result = await dependencies.get_current_user(request(backend, cookies={SESSION_COOKIE_NAME: "session"}), None)
    assert result == UserContext("session-user", [], "user", "settings-ns", True, session_id="session")
    args, kwargs = backend.oauth.resolve_active_session.await_args
    assert args == (backend.tx, "session")
    assert kwargs["now"].tzinfo == timezone.utc
    backend.oauth.lookup_api_key.assert_not_awaited()
    backend.oauth.resolve_active_session.return_value = None
    for cookies in ({SESSION_COOKIE_NAME: "expired"}, {}):
        with pytest.raises(HTTPException) as exc:
            await dependencies.get_current_user(request(backend, cookies=cookies), None)
        assert (exc.value.status_code, exc.value.detail) == (401, "Authentication required")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,allowed",
    [
        ("get", "/stats", True),
        ("GET", "/v1/federation/status", True),
        ("POST", "/stats", False),
        ("GET", "/data", False),
    ],
)
async def test_monitor_route_boundary(auth, backend, method, path, allowed):
    dependencies.configure_auth({"touch_interval_seconds": 0})
    backend.oauth.lookup_api_key.return_value = key_row(role="monitor")
    if allowed:
        assert (await dependencies.get_current_user(request(backend, method, path), credential())).role == "monitor"
    else:
        with pytest.raises(HTTPException) as exc:
            await dependencies.get_current_user(request(backend, method, path), credential())
        assert (exc.value.status_code, exc.value.detail) == (403, "Monitor access is limited to status reads")


@pytest.mark.asyncio
async def test_unknown_role_rejected(auth, backend):
    dependencies.configure_auth({"touch_interval_seconds": 0})
    backend.oauth.lookup_api_key.return_value = key_row(role="unknown")
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(request(backend), credential())
    assert (exc.value.status_code, exc.value.detail) == (403, "Role not permitted")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,root_allowed,monitor_allowed",
    [
        ("root", True, True),
        ("monitor", False, True),
        ("user", False, False),
        ("unknown", False, False),
    ],
)
async def test_role_dependencies(role, root_allowed, monitor_allowed):
    context = user(role)
    for dependency, allowed, detail in (
        (dependencies.require_root, root_allowed, "Root access required"),
        (dependencies.require_root_or_monitor, monitor_allowed, "Root or monitor access required"),
    ):
        if allowed:
            assert await dependency(context) is context
        else:
            with pytest.raises(HTTPException) as exc:
                await dependency(context)
            assert (exc.value.status_code, exc.value.detail) == (403, detail)


@pytest.mark.asyncio
async def test_touch_is_debounced_and_flushed(auth, backend, monkeypatch):
    clock = SimpleNamespace(monotonic=lambda: 100.0)
    monkeypatch.setattr(dependencies, "time", clock)
    tasks = []

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(lifecycle, "_schedule_background", schedule)
    backend.oauth.lookup_api_key.return_value = key_row()
    await dependencies.get_current_user(request(backend), credential())
    await dependencies.get_current_user(request(backend), credential())
    assert len(tasks) == 1
    assert dependencies.auth_cache_stats().pending_touches == 1
    await tasks[0]
    backend.oauth.touch_api_key.assert_awaited_once_with(backend.tx, "key-1")
    assert dependencies.auth_cache_stats().pending_touches == 0
    clock.monotonic = lambda: 160.0
    await dependencies.get_current_user(request(backend), credential())
    await tasks[1]
    assert backend.oauth.touch_api_key.await_count == 2


@pytest.mark.asyncio
async def test_replica_auth_does_not_schedule_writes(auth, backend, monkeypatch):
    monkeypatch.setattr(replica_mode, "replica_mode_enabled", lambda: True)
    backend.oauth.lookup_api_key.return_value = key_row()
    assert (await dependencies.get_current_user(request(backend), credential())).authenticated
    assert dependencies.auth_cache_stats().pending_touches == 0
    backend.oauth.touch_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_touch_is_retried_only_after_interval(auth, backend, monkeypatch):
    clock = SimpleNamespace(monotonic=lambda: 100.0)
    monkeypatch.setattr(dependencies, "time", clock)
    tasks = []

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(lifecycle, "_schedule_background", schedule)
    backend.oauth.lookup_api_key.return_value = key_row()
    backend.oauth.touch_api_key.side_effect = [RuntimeError("write failed"), None]
    await dependencies.get_current_user(request(backend), credential())
    await tasks[0]
    assert dependencies.auth_cache_stats().pending_touches == 0
    await dependencies.get_current_user(request(backend), credential())
    assert len(tasks) == 1
    clock.monotonic = lambda: 160.0
    await dependencies.get_current_user(request(backend), credential())
    await tasks[1]
    assert backend.oauth.touch_api_key.await_count == 2
