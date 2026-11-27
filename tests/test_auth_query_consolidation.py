from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


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
    # The M5 cache/touch state is module-global: a test earlier in this file
    # (or in another module) may have minted an entry for this same key
    # hash under the real monotonic clock, and the module's default
    # _touch_interval (60 s) can still suppress this fresh touch. Reset so
    # the cache is empty and the debounce clock starts here, not in the
    # previous test's timeline.
    auth_mod._reset_auth_caches()
    clock = [time.monotonic()]
    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: clock[0])

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


def test_touch_flusher_clears_its_handle_after_a_failed_write(monkeypatch):
    """A touch queued during a failing flush must not leave a dangling handle.

    The failing batch is debounced back to ``_touch_times`` (no hot retry)
    while a key queued concurrently stays queued for the single chained
    successor the flusher's exit path schedules. Either way the flusher
    drops its own handle — no stale reference to a finished task.
    """
    auth_mod, backend, _oauth = _auth_env(monkeypatch, dict(_KEY_ROW))

    started = asyncio.Event()
    fail = asyncio.Event()
    touched: list[str] = []

    async def _boom(_backend, _oauth, key_ids):
        touched.extend(key_ids)
        started.set()
        await fail.wait()
        raise RuntimeError("store down")

    monkeypatch.setattr(auth_mod, "_write_touches", _boom)

    async def _run():
        auth_mod._dirty_keys.clear()
        auth_mod._touch_task = None
        auth_mod._dirty_keys["key-1"] = None

        task = asyncio.create_task(auth_mod._flush_dirty_touches(backend, backend.oauth))
        await started.wait()
        # A concurrent request queues another touch while the write is failing.
        auth_mod._dirty_keys["key-2"] = None
        auth_mod._touch_task = task
        fail.set()
        await task
        chained = auth_mod._touch_task
        if chained is not None and chained is not task:
            chained.cancel()
            try:
                await chained
            except (asyncio.CancelledError, Exception):
                pass
        return list(auth_mod._dirty_keys)

    remaining = asyncio.run(_run())

    assert touched == ["key-1", "key-2"], (
        "the exit path must chain one successor that drains the late key "
        "instead of stranding it for an unrelated later request"
    )
    assert remaining == [], "a failed write debounces (no hot retry), it does not re-queue"
    assert auth_mod._touch_task is None, "the flusher must drop its handle on every exit path"


@pytest.mark.asyncio
async def test_touch_flush_is_single_flight_under_a_slow_store(monkeypatch):
    """Many touches across loop turns start at most one flusher transaction.

    The flusher blocks inside its first transaction while N distinct keys
    arrive on separate loop turns. Genuinely single-flight: the shared
    handle is reused, never duplicated, so no second ``transactional()``
    overlaps the first.
    """
    import mnemos.api.dependencies as auth_mod
    import mnemos.core.lifecycle as lc

    entered = 0
    max_active = 0
    active = 0
    release = asyncio.Event()
    written: list[str] = []

    @asynccontextmanager
    async def _tx_cm():
        nonlocal entered, max_active, active
        entered += 1
        active += 1
        max_active = max(max_active, active)
        try:
            yield _Tx()
        finally:
            active -= 1

    class _SlowOAuth(_FakeOAuthRepo):
        async def touch_api_key(self, _tx, key_id):
            written.append(str(key_id))

    oauth = _SlowOAuth(api_key_row=dict(_KEY_ROW))
    real_write = auth_mod._write_touches

    async def _slow_write(backend, repo, key_ids):
        if entered == 0:
            await release.wait()
        await real_write(backend, repo, key_ids)

    monkeypatch.setattr(auth_mod, "_write_touches", _slow_write)
    backend = SimpleNamespace(oauth=oauth, transactional=_tx_cm)
    real_schedule = lc._schedule_background
    monkeypatch.setattr(lc, "_schedule_background", lambda coro: asyncio.ensure_future(coro))

    monkeypatch.setattr(auth_mod, "_touch_interval", 0.0001)
    auth_mod._reset_auth_caches()
    auth_mod._touch_task = None
    try:
        auth_mod._schedule_touch(backend, oauth, "key-slow")
        for i in range(10):
            await asyncio.sleep(0)
            auth_mod._schedule_touch(backend, oauth, f"key-{i}")
        release.set()
        for _ in range(200):
            await asyncio.sleep(0)
            if not auth_mod._dirty_keys and not auth_mod._task_live(auth_mod._touch_task):
                break
        for _ in range(50):
            await asyncio.sleep(0)
        assert entered >= 1, "the flusher must have run"
        assert max_active == 1, f"expected a single in-flight flusher transaction, saw {max_active}"
        assert sorted(written) == sorted(["key-slow"] + [f"key-{i}" for i in range(10)])
        assert list(auth_mod._dirty_keys) == []
    finally:
        auth_mod._reset_auth_caches()
        auth_mod._touch_task = None
        monkeypatch.setattr(lc, "_schedule_background", real_schedule, raising=False)


@pytest.mark.asyncio
async def test_touch_flusher_chains_a_successor_for_late_keys(monkeypatch):
    """A key landing after the final snapshot is flushed with no later request.

    The flusher snapshots ``key-1``, the late key lands while the write is
    still in flight, and the loop's next turn drains it in the same task
    (single-flight needs no second task for this). The assertion that
    matters: after the original task is fully done, the queue is empty and
    the shared handle is dropped — the old code left ``key-late``
    stranded whenever it arrived after the loop's last snapshot, and every
    task blindly cleared the shared handle even when a successor owned it.
    """
    import mnemos.api.dependencies as auth_mod
    import mnemos.core.lifecycle as lc

    backend, oauth = _backend(dict(_KEY_ROW))
    started = asyncio.Event()
    release = asyncio.Event()
    real_write = auth_mod._write_touches
    flushers_started = 0
    orig_ensure = auth_mod._ensure_touch_flusher

    def _counting_ensure(b, o):
        nonlocal flushers_started
        flushers_started += 1
        return orig_ensure(b, o)

    async def _gated_write(_backend, _oauth, key_ids):
        started.set()
        await release.wait()
        await real_write(_backend, _oauth, key_ids)

    monkeypatch.setattr(auth_mod, "_write_touches", _gated_write)
    monkeypatch.setattr(auth_mod, "_ensure_touch_flusher", _counting_ensure)
    monkeypatch.setattr(lc, "_schedule_background", lambda coro: asyncio.ensure_future(coro))
    auth_mod._reset_auth_caches()
    auth_mod._touch_task = None
    try:
        auth_mod._dirty_keys["key-1"] = None
        task = asyncio.ensure_future(auth_mod._flush_dirty_touches(backend, backend.oauth))
        auth_mod._touch_task = task
        await started.wait()
        # Land after the snapshot, while the write is still in flight.
        auth_mod._dirty_keys["key-late"] = None
        release.set()
        await task
        for _ in range(200):
            await asyncio.sleep(0)
            handle = auth_mod._touch_task
            if handle is None or (hasattr(handle, "done") and handle.done()):
                break
        old = auth_mod._touch_task
        if old is not None and hasattr(old, "done") and not old.done():
            await asyncio.wait_for(asyncio.shield(old), timeout=5)
        assert sorted(oauth.touch_calls) == ["key-1", "key-late"]
        assert list(auth_mod._dirty_keys) == []
        assert flushers_started <= 1, (
            "the in-loop drain must absorb the late key; "
            f"a second flusher ({flushers_started} chained) means single-flight regressed"
        )
    finally:
        handle = auth_mod._touch_task
        if handle is not None and hasattr(handle, "cancel") and hasattr(handle, "done"):
            try:
                if not handle.done():
                    handle.cancel()
            except Exception:
                pass
        auth_mod._reset_auth_caches()
        auth_mod._touch_task = None


@pytest.mark.asyncio
async def test_touch_flusher_cancel_leaves_no_leaked_tasks(monkeypatch):
    """Shutdown-cancel of the flusher strands nothing and leaks no tasks."""
    import mnemos.api.dependencies as auth_mod

    backend, _oauth = _backend(dict(_KEY_ROW))
    before = {t for t in asyncio.all_tasks()}
    auth_mod._reset_auth_caches()
    auth_mod._touch_task = None

    started = asyncio.Event()
    release = asyncio.Event()

    async def _hung(_backend, _oauth, key_ids):
        started.set()
        await release.wait()

    monkeypatch.setattr(auth_mod, "_write_touches", _hung)
    try:
        auth_mod._dirty_keys["key-1"] = None
        task = asyncio.ensure_future(auth_mod._flush_dirty_touches(backend, backend.oauth))
        auth_mod._touch_task = task
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        assert auth_mod._touch_task is None or auth_mod._touch_task.done()
        # The cancelled in-flight batch is re-offered with debounce backoff
        # (the failure path's _requeue_touch), not dropped and not hot:
        # the key stays queued, its timestamp is reset to "now", and the
        # flusher chained nothing (no successor task on the exit path).
        assert list(auth_mod._dirty_keys) == ["key-1"], (
            "a cancelled batch must stay queued for the next request, not be lost"
        )
        assert "key-1" in auth_mod._touch_times, "cancellation must debounce like failure, not retry immediately"
        assert auth_mod._touch_task is None, "cancellation must never chain a successor"
        leaked = {t for t in asyncio.all_tasks() if not t.done()} - before
        assert leaked == set(), f"leaked flusher tasks: {leaked}"
    finally:
        release.set()
        auth_mod._reset_auth_caches()
        auth_mod._touch_task = None


@pytest.mark.asyncio
async def test_unreadable_flusher_handle_recovers_instead_of_stall(monkeypatch):
    """A live handle that refuses to answer must not strand the queue forever.

    ``_task_live`` is the sole gate on the shared handle; treating an
    unreadable handle as live would make every later touch return True
    without scheduling, and nothing would ever drain the queue. The guard
    must recover: drop the stale reference and let the next touch start a
    fresh flusher.
    """
    import mnemos.api.dependencies as auth_mod

    class _BadHandle:
        def done(self):
            raise RuntimeError("unavailable")

    backend, oauth = _backend(dict(_KEY_ROW))
    auth_mod._reset_auth_caches()
    auth_mod._touch_task = None
    flushers_started = 0
    orig_ensure = auth_mod._ensure_touch_flusher

    def _counting_ensure(b, o):
        nonlocal flushers_started
        flushers_started += 1
        return orig_ensure(b, o)

    monkeypatch.setattr(auth_mod, "_ensure_touch_flusher", _counting_ensure)
    import mnemos.core.lifecycle as lc

    monkeypatch.setattr(lc, "_schedule_background", lambda coro: asyncio.ensure_future(coro))
    try:
        # The queue holds a key, but the shared handle is unreadable.
        auth_mod._dirty_keys["key-bad"] = None
        auth_mod._touch_task = _BadHandle()
        assert auth_mod._task_live(auth_mod._touch_task) is False

        # The guard sees no live flusher and starts a fresh one, which
        # drains the queued key — recovery, not a permanent stall.
        assert auth_mod._ensure_touch_flusher(backend, oauth) is True
        for _ in range(200):
            await asyncio.sleep(0)
            if not auth_mod._dirty_keys and auth_mod._task_live(auth_mod._touch_task) is False:
                break
        await asyncio.sleep(0)
        assert flushers_started == 1
        assert oauth.touch_calls == ["key-bad"], "the recovered flusher must drain the queue"
        assert list(auth_mod._dirty_keys) == []
    finally:
        handle = auth_mod._touch_task
        if handle is not None and hasattr(handle, "cancel") and hasattr(handle, "done"):
            try:
                if not handle.done():
                    handle.cancel()
            except Exception:
                pass
        auth_mod._reset_auth_caches()
        auth_mod._touch_task = None
        monkeypatch.setattr(lc, "_schedule_background", None, raising=False)
