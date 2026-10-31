from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock


class _Tx:
    pass


class _FakeOAuthRepo:
    def __init__(self, api_key_row=None):
        self._api_key_row = api_key_row
        self.lookup_calls = 0
        self.touch_calls: list[str] = []

    async def lookup_api_key(self, _tx, key_hash):
        self.lookup_calls += 1
        return self._api_key_row

    async def touch_api_key(self, _tx, key_id):
        self.touch_calls.append(str(key_id))

    async def resolve_active_session(self, _tx, _sid, *, now):
        return None


def _backend(api_key_row):
    @asynccontextmanager
    async def _tx_cm():
        yield _Tx()

    oauth = _FakeOAuthRepo(api_key_row=api_key_row)
    return SimpleNamespace(
        oauth=oauth,
        transactional=_tx_cm,
        _supports_oauth_persistence=True,
    ), oauth


def test_api_key_auth_fetches_user_and_groups_in_one_query(monkeypatch):
    """The API-key auth path goes through ``OAuthRepository.lookup_api_key``,
    which returns a denormalised row (api_keys + users + aggregated
    user_groups) in a single round-trip on every supported backend.
    """
    import mnemos.api.dependencies as auth_mod
    from mnemos.api.dependencies import get_current_user

    api_key_row = {
        "id": "key-1",
        "user_id": "alice",
        "revoked": False,
        "role": "user",
        "namespace": "alice-ns",
        "group_ids": ["ops", "dev"],
    }
    backend, oauth = _backend(api_key_row)

    monkeypatch.setattr(auth_mod, "_auth_enabled", True)

    import mnemos.core.lifecycle as lc

    def _run_bg(coro):
        try:
            while True:
                coro.send(None)
        except StopIteration:
            pass
        finally:
            coro.close()
        return None

    monkeypatch.setattr(lc, "_schedule_background", _run_bg)

    request = MagicMock()
    request.app.state.persistence_backend = backend
    request.cookies = {}

    creds = MagicMock()
    creds.credentials = "test-key"

    user = asyncio.run(get_current_user(request, creds))

    assert user.user_id == "alice"
    assert user.group_ids == ["ops", "dev"]
    assert oauth.lookup_calls == 1
    assert oauth.touch_calls == ["key-1"]


# ── Decision M5: per-request auth cost. The base ran one ``lookup_api_key``
# transaction plus a ``touch_api_key`` write on EVERY bearer request.


def _auth_env(monkeypatch, api_key_row, *, cache_ttl=30.0, touch_interval=60.0):
    # (clock advanced by tests through ``_advance`` on the returned module dict)
    """Configure auth with explicit M5 knobs and a running-loop background stub."""
    import datetime

    import mnemos.api.dependencies as auth_mod
    import mnemos.core.lifecycle as lc

    backend, oauth = _backend(api_key_row)
    monkeypatch.setattr(auth_mod, "_auth_enabled", True)
    monkeypatch.setattr(auth_mod, "_cache_ttl", cache_ttl)
    monkeypatch.setattr(auth_mod, "_touch_interval", touch_interval)

    # Give the monotonic clock a real base so "now - interval" is meaningful.
    clock = [datetime.datetime.now().timestamp()]
    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: clock[0])
    auth_mod._reset_auth_caches()

    scheduled = []

    def _run_bg(coro):
        scheduled.append(coro)
        try:
            while True:
                coro.send(None)
        except StopIteration:
            pass
        finally:
            coro.close()
        return None

    monkeypatch.setattr(lc, "_schedule_background", _run_bg)
    auth_mod._test_clock = clock
    return auth_mod, backend, oauth


def _bearer(backend, token="test-key"):
    request = MagicMock()
    request.app.state.persistence_backend = backend
    request.cookies = {}
    creds = MagicMock()
    creds.credentials = token
    return request, creds


def _call(auth_mod, backend, token="test-key"):
    request, creds = _bearer(backend, token)
    return asyncio.run(auth_mod.get_current_user(request, creds))


_KEY_ROW = {
    "id": "key-1",
    "user_id": "alice",
    "revoked": False,
    "role": "user",
    "namespace": "alice-ns",
    "group_ids": ["ops"],
}


def test_repeated_requests_hit_a_short_ttl_cache(monkeypatch):
    """N requests inside the TTL cost one lookup and at most one touch."""
    auth_mod, backend, oauth = _auth_env(monkeypatch, dict(_KEY_ROW))

    for _ in range(5):
        assert _call(auth_mod, backend).user_id == "alice"

    assert oauth.lookup_calls == 1, "the key hash -> UserContext cache must absorb repeats"
    assert len(oauth.touch_calls) == 1, "last_used touches must be debounced, not per request"


def test_cache_entries_are_independent_copies(monkeypatch):
    """A route that mutates user.group_ids must not poison later requests."""
    auth_mod, backend, _oauth = _auth_env(monkeypatch, dict(_KEY_ROW))

    first = _call(auth_mod, backend)
    first.group_ids.append("injected")

    assert _call(auth_mod, backend).group_ids == ["ops"]


def test_touch_count_is_bounded_by_the_touch_interval(monkeypatch):
    """K requests inside one interval produce exactly one last_used write."""
    auth_mod, backend, oauth = _auth_env(monkeypatch, dict(_KEY_ROW), cache_ttl=0.0, touch_interval=60.0)

    for _ in range(20):
        _call(auth_mod, backend)

    assert oauth.lookup_calls == 20, "cache_ttl=0 must disable the credential cache"
    assert len(oauth.touch_calls) == 1


def test_revocation_is_visible_on_the_same_worker_after_invalidate(monkeypatch):
    """Revoke then re-authenticate: 401 at once, not "valid for the TTL"."""
    auth_mod, backend, _oauth = _auth_env(monkeypatch, dict(_KEY_ROW))

    assert _call(auth_mod, backend).user_id == "alice"
    assert auth_mod.auth_cache_stats().key_cache_entries == 1

    backend.oauth._api_key_row = {**_KEY_ROW, "revoked": True}
    auth_mod.invalidate_api_key_cache(key_id="key-1")

    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        _call(auth_mod, backend)
    assert excinfo.value.status_code == 401


def test_cache_entry_expires_after_the_ttl(monkeypatch):
    """The TTL is the documented cross-worker revocation lag — and it holds."""
    auth_mod, backend, oauth = _auth_env(monkeypatch, dict(_KEY_ROW), cache_ttl=30.0)

    assert _call(auth_mod, backend).user_id == "alice"
    assert oauth.lookup_calls == 1

    backend.oauth._api_key_row = {**_KEY_ROW, "revoked": True}
    auth_mod._test_clock[0] += 31.0  # one second past the TTL

    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        _call(auth_mod, backend)
    assert excinfo.value.status_code == 401
