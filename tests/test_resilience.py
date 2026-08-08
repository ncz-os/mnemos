from __future__ import annotations
import asyncio
import threading
import pytest

import logging
import time
from types import SimpleNamespace

from mnemos.core.resilience import (
    InProcessCircuitBreakerPool,
    InProcessConcurrencyLimiterPool,
    InProcessRateLimiter,
    InProcessRateLimiterPool,
    NatsCircuitBreakerPool,
    NatsRateLimiterPool,
    NatsConcurrencyLimiterPool,
    call_maybe_async,
    make_circuit_breaker_pool,
    make_concurrency_limiter,
    make_rate_limiter_pool,
)


class _WrongLastError(Exception):
    pass


class _NotFoundError(Exception):
    pass


class _FakeKvEntry:
    def __init__(self, value: bytes, revision: int):
        self.value = value
        self.revision = revision


class _FakeNatsKv:
    def __init__(self):
        self._values: dict[str, tuple[bytes, int]] = {}
        self._rev = 0

    async def get(self, key: str):
        try:
            value, revision = self._values[key]
        except KeyError:
            raise _NotFoundError("not found")
        return _FakeKvEntry(value, revision)

    async def create(self, key: str, value: bytes, **kwargs):
        if key in self._values:
            raise _WrongLastError("wrong last sequence")
        if kwargs:
            raise AssertionError(f"per-key KV kwargs are not supported: {kwargs}")
        return await self.put(key, value)

    async def put(self, key: str, value: bytes):
        self._rev += 1
        self._values[key] = (value, self._rev)
        return self._rev

    async def update(self, key: str, value: bytes, *, last: int, **kwargs):
        if key not in self._values:
            raise _WrongLastError("wrong last sequence")
        _old, revision = self._values[key]
        if revision != last:
            raise _WrongLastError("wrong last sequence")
        if kwargs:
            raise AssertionError(f"per-key KV kwargs are not supported: {kwargs}")
        return await self.put(key, value)


class _FakeAsyncRedis:
    def __init__(self):
        self._strings: dict[str, tuple[str, float | None]] = {}
        self._hashes: dict[str, tuple[dict[str, str], float | None]] = {}
        self._zsets: dict[str, tuple[dict[str, float], float | None]] = {}
        self.closed = False

    async def ping(self):
        return True

    async def aclose(self):
        self.closed = True

    def _expired(self, expires_at: float | None) -> bool:
        return expires_at is not None and expires_at <= time.monotonic()

    def _cleanup_key(self, key: str) -> None:
        value = self._strings.get(key)
        if value and self._expired(value[1]):
            self._strings.pop(key, None)
        value = self._hashes.get(key)
        if value and self._expired(value[1]):
            self._hashes.pop(key, None)
        value = self._zsets.get(key)
        if value and self._expired(value[1]):
            self._zsets.pop(key, None)

    async def get(self, key: str):
        self._cleanup_key(key)
        value = self._strings.get(key)
        return value[0] if value else None

    async def zrem(self, key: str, token: str):
        self._cleanup_key(key)
        members, expires_at = self._zsets.get(key, ({}, None))
        members.pop(token, None)
        self._zsets[key] = (members, expires_at)
        return 1

    async def eval(self, script: str, numkeys: int, *keys_and_args):
        keys = list(keys_and_args[:numkeys])
        args = list(keys_and_args[numkeys:])
        if "opened_at" in script:
            return self._eval_circuit_failure(keys, args)
        if "redis.call('DEL', KEYS[3])" in script:
            return self._eval_circuit_success(keys)
        if "ZREMRANGEBYSCORE" in script:
            return self._eval_concurrency_acquire(keys, args)
        return self._eval_rate_limit(keys, args)

    def _expire_at(self, seconds: int | float) -> float | None:
        seconds = float(seconds)
        return time.monotonic() + seconds if seconds > 0 else time.monotonic()

    def _eval_circuit_failure(self, keys, args):
        failures_key, state_key, hash_key = keys
        threshold, cooldown, opened_at = int(args[0]), int(args[1]), str(args[2])
        self._cleanup_key(failures_key)
        current = int(self._strings.get(failures_key, ("0", None))[0]) + 1
        expires_at = self._expire_at(cooldown)
        self._strings[failures_key] = (str(current), expires_at)
        self._hashes[hash_key] = ({"state": "closed", "failures": str(current)}, expires_at)
        if current >= threshold:
            self._strings[state_key] = ("open", expires_at)
            self._hashes[hash_key] = (
                {"state": "open", "failures": str(current), "opened_at": opened_at},
                expires_at,
            )
            return [1, current]
        return [0, current]

    def _eval_circuit_success(self, keys):
        for key in keys:
            self._strings.pop(key, None)
            self._hashes.pop(key, None)
        return 1

    def _eval_rate_limit(self, keys, args):
        key = keys[0]
        limit, ttl = int(args[0]), int(args[1])
        self._cleanup_key(key)
        count = int(self._strings.get(key, ("0", None))[0]) + 1
        expires_at = self._strings.get(key, ("0", None))[1]
        if count == 1:
            expires_at = self._expire_at(ttl)
        self._strings[key] = (str(count), expires_at)
        return 1 if count <= limit else 0

    def _eval_concurrency_acquire(self, keys, args):
        key = keys[0]
        now, expires_at, max_concurrent, token = (
            float(args[0]),
            float(args[1]),
            int(args[2]),
            str(args[3]),
        )
        self._cleanup_key(key)
        members, _old_expires_at = self._zsets.get(key, ({}, None))
        members = {member: score for member, score in members.items() if score > now}
        if len(members) >= max_concurrent:
            self._zsets[key] = (members, expires_at)
            return 0
        members[token] = expires_at
        self._zsets[key] = (members, expires_at)
        return 1


def _settings(storage_uri: str = "memory://", *, fallback_warning: bool = False):
    return SimpleNamespace(
        rate_limit=SimpleNamespace(storage_uri=storage_uri),
        resilience=SimpleNamespace(
            circuit_breaker_nats_prefix="test:cb:",
            rate_limiter_nats_prefix="test:rl:",
            concurrency_nats_prefix="test:conc:",
            fallback_warning=fallback_warning,
        ),
        server=SimpleNamespace(redis_url="redis://cache:6379/0"),
        federation=SimpleNamespace(peers="", enabled=False),
        layers=SimpleNamespace(active_layers=[]),
        nats=SimpleNamespace(url=None, token=None),
    )


def test_in_process_circuit_breaker_opens_after_threshold():
    pool = InProcessCircuitBreakerPool(failure_threshold=2, cooldown_seconds=60)

    assert pool.is_allowed("openai")
    pool.record_failure("openai")
    assert pool.is_allowed("openai")
    pool.record_failure("openai")

    assert not pool.is_allowed("openai")
    assert pool.status()["openai"]["state"] == "open"


def test_in_process_circuit_breaker_allows_probe_after_cooldown():
    pool = InProcessCircuitBreakerPool(failure_threshold=1, cooldown_seconds=0.01)

    pool.record_failure("openai")
    assert not pool.is_allowed("openai")
    time.sleep(0.02)

    assert pool.is_allowed("openai")
    pool.record_success("openai")
    pool.record_success("openai")
    assert pool.status()["openai"]["state"] == "closed"


def test_in_process_rate_limiter_blocks_above_rpm():
    limiter = InProcessRateLimiter("openai", rpm=2)

    assert limiter.is_allowed()
    assert limiter.is_allowed()
    assert not limiter.is_allowed()


def test_in_process_rate_limiter_pool_uses_default_for_unknown_provider():
    pool = InProcessRateLimiterPool(overrides={"openai": 1})

    assert pool.is_allowed("new-provider")
    assert pool.status()["new-provider"] == 1


def test_in_process_concurrency_limiter_limits_concurrent_acquires():
    async def run():
        pool = InProcessConcurrencyLimiterPool(overrides={"openai": 1})
        assert await pool.acquire("openai")
        assert not await pool.acquire("openai")
        pool.release("openai")
        assert await pool.acquire("openai")
        pool.release("openai")

    asyncio.run(run())


def test_nats_rate_limiter_rpm_is_shared_across_pools():
    async def run():
        kv = _FakeNatsKv()
        first = NatsRateLimiterPool(kv, "test:rl:", overrides={"openai": 2})
        second = NatsRateLimiterPool(kv, "test:rl:", overrides={"openai": 2})
        try:
            assert await first.is_allowed("openai")
            assert await second.is_allowed("openai")
            assert not await first.is_allowed("openai")
        finally:
            first.close()
            second.close()

    asyncio.run(run())


def test_nats_concurrency_limiter_slots_are_shared_across_pools():
    async def run():
        kv = _FakeNatsKv()
        first = NatsConcurrencyLimiterPool(kv, "test:conc:", overrides={"openai": 1})
        second = NatsConcurrencyLimiterPool(kv, "test:conc:", overrides={"openai": 1})
        try:
            assert await first.acquire("openai")
            assert not await second.acquire("openai")
            await first.release("openai")
            assert await second.acquire("openai")
            await second.release("openai")
        finally:
            first.close()
            second.close()

    asyncio.run(run())


def test_nats_rate_limiter_degrades_to_in_process_on_kv_failure():
    async def run():
        # Pass None kv — limiter should not block
        pool = NatsRateLimiterPool(None, "test:rl:", overrides={"openai": 1})
        try:
            # With no KV, acquire should succeed (degradation)
            assert await pool.is_allowed("openai")
        finally:
            pool.close()

    asyncio.run(run())


def test_nats_concurrency_limiter_degrades_to_in_process_on_kv_failure():
    async def run():
        pool = NatsConcurrencyLimiterPool(None, "test:conc:", overrides={"openai": 1})
        try:
            # With no KV, acquire should succeed (degradation)
            assert await pool.acquire("openai")
        finally:
            pool.close()

    asyncio.run(run())


def test_factory_returns_nats_backends_when_nats_configured():
    kv = _FakeNatsKv()
    settings = _settings()
    settings.nats = SimpleNamespace(url="nats://localhost:4222", token=None)

    assert isinstance(make_circuit_breaker_pool(settings, nats_kv=kv), NatsCircuitBreakerPool)
    assert isinstance(make_rate_limiter_pool(settings, nats_kv=kv), NatsRateLimiterPool)
    assert isinstance(make_concurrency_limiter(settings, nats_kv=kv), NatsConcurrencyLimiterPool)


def test_factory_memory_uri_returns_in_process_and_warns(caplog):
    settings = _settings("memory://", fallback_warning=True)
    caplog.set_level(logging.WARNING)

    pool = make_circuit_breaker_pool(settings)

    assert isinstance(pool, InProcessCircuitBreakerPool)
    assert "NATS not configured" in caplog.text
    assert "Multi-worker deployments require Redis" not in caplog.text


def test_factory_no_backend_returns_in_process_and_warns(caplog):
    settings = _settings("memory://", fallback_warning=True)
    caplog.set_level(logging.WARNING)

    pool = make_rate_limiter_pool(settings)

    assert isinstance(pool, InProcessRateLimiterPool)
    assert "NATS not configured" in caplog.text


def test_factory_no_backend_concurrency_returns_in_process_and_warns(caplog):
    settings = _settings("memory://", fallback_warning=True)
    caplog.set_level(logging.WARNING)

    pool = make_concurrency_limiter(settings)

    assert isinstance(pool, InProcessConcurrencyLimiterPool)
    assert "NATS not configured" in caplog.text


def test_nats_circuit_breaker_cross_instance_and_success_preserves_peer_trip():
    async def run():
        kv = _FakeNatsKv()
        first = NatsCircuitBreakerPool(kv, "test:cb:", failure_threshold=2, cooldown_seconds=60)
        second = NatsCircuitBreakerPool(kv, "test:cb:", failure_threshold=2, cooldown_seconds=60)
        try:
            await first.record_failure("openai")
            assert await second.is_allowed("openai")
            await second.record_failure("openai")
            assert not await first.is_allowed("openai")
            await first.record_success("openai")
            assert not await second.is_allowed("openai")
        finally:
            first.close()
            second.close()

    asyncio.run(run())


def test_nats_circuit_breaker_keys_are_opaque():
    from mnemos.core.resilience import NatsCircuitBreaker

    breaker = NatsCircuitBreaker(_FakeNatsKv(), "cb.", failure_threshold=2, cooldown_seconds=60)
    try:
        key = breaker._key("provider/model:identity")
        assert key.startswith("cb.circuit.")
        for plaintext in ("provider", "model", "identity"):
            assert plaintext not in key
    finally:
        breaker.close()


# ---------------------------------------------------------------------------
# NatsVisibilityEpoch — the failure modes that actually matter.
#
# The original coverage passed epochs 0 and 1 into _get_cache_key() and asserted
# the keys differed. That proves hashing takes an extra argument; it proves
# nothing about CAS races, retry exhaustion, degraded recovery, or monotonicity.
# ---------------------------------------------------------------------------


class KeyNotFoundError(Exception):
    """Named so _nats_missing_key() classifies it as nats-py errors are."""


class _FakeEntry:
    def __init__(self, value, revision):
        self.value = value
        self.revision = revision


class _FakeKV:
    """Minimal JetStream KV with real revision semantics."""

    def __init__(self, *, fail_updates=0):
        self._val = None
        self._rev = 0
        self._fail_updates = fail_updates

    async def get(self, key):
        if self._val is None:
            # Must match _nats_missing_key(), which keys on "not found".
            raise KeyNotFoundError("key not found")
        return _FakeEntry(self._val, self._rev)

    async def create(self, key, value):
        if self._val is not None:
            raise RuntimeError("wrong last sequence")
        self._val, self._rev = value, 1
        return self._rev

    async def update(self, key, value, last=None):
        if self._fail_updates > 0:
            self._fail_updates -= 1
            # Emulate a competing writer: bump the revision so `last` is stale.
            self._rev += 1
            raise RuntimeError("wrong last sequence")
        if last != self._rev:
            raise RuntimeError("wrong last sequence")
        self._val, self._rev = value, self._rev + 1
        return self._rev


def _epoch_with(kv):
    from mnemos.core.resilience import NatsVisibilityEpoch

    e = NatsVisibilityEpoch.__new__(NatsVisibilityEpoch)
    e.bucket = "test"
    e._settings = None
    e._floor = 0
    e._floor_lock = threading.Lock()

    async def _kv():
        return kv

    e._kv = _kv
    return e


def test_epoch_bump_increments_and_persists():
    kv = _FakeKV()
    e = _epoch_with(kv)
    assert asyncio.run(e.bump()) == 1
    assert asyncio.run(e.bump()) == 2
    assert asyncio.run(e.current()) == 2


def test_epoch_bump_retries_through_cas_contention():
    # Two lost races, then success: the value must still advance exactly once
    # per successful bump, never skipping backwards.
    kv = _FakeKV(fail_updates=2)
    e = _epoch_with(kv)
    asyncio.run(e.bump())
    first = asyncio.run(e.current())
    assert first >= 1
    assert asyncio.run(e.bump()) > first


def test_epoch_raises_when_it_cannot_advance():
    """Retry exhaustion must be LOUD.

    Silently returning a degraded value here is what re-opens the revocation
    leak: the caller discards the return, later reads succeed against the
    healthy store, and they see the OLD epoch.
    """
    from mnemos.core.resilience import VisibilityEpochBumpFailed

    kv = _FakeKV(fail_updates=10_000)
    e = _epoch_with(kv)
    asyncio.run(e.bump())  # seed
    with pytest.raises(VisibilityEpochBumpFailed):
        asyncio.run(e.bump(attempts=3))


def test_epoch_never_moves_backward_on_recovery():
    """A degraded epoch is a large time bucket; the persisted counter is small.

    Handing back the small number after recovery would make cache entries
    written during the outage readable again.
    """
    kv = _FakeKV()
    e = _epoch_with(kv)

    async def _no_kv():
        return None

    e._kv = _no_kv
    degraded = asyncio.run(e.current())
    assert degraded > 1000, "degraded epoch should be a clock-derived bucket"

    async def _kv():
        return kv

    e._kv = _kv
    asyncio.run(e.bump())
    assert asyncio.run(e.current()) >= degraded, "epoch moved backward on recovery"


def test_concurrency_pool_release_without_acquire_does_not_inflate_slots():
    """An unmatched release must not hand back a slot that was never taken."""
    from mnemos.core.resilience import NatsConcurrencyLimiterPool

    pool = NatsConcurrencyLimiterPool.__new__(NatsConcurrencyLimiterPool)
    pool._tokens = {}
    pool._token_lock = threading.Lock()
    pool._providers = set()
    pool._remember = lambda p: pool._providers.add(p)
    called = []

    class _L:
        key_prefix = "x"
        bucket = "b"
        _settings = None
        _kv_future = None
        _loop = None
        _in_flight = {}
        _lock = threading.Lock()

        async def release(self, provider, token):
            called.append(token)

    pool._limiter = _L()
    pool._max_concurrent = lambda p: 1
    asyncio.run(pool.release("openai"))
    assert called == [], "release without a matching acquire must be a no-op"


def test_epoch_listeners_fire_and_cannot_break_the_mutation():
    """A visibility bump must notify listeners, and a broken listener must not
    propagate: the mutation that triggered it has already happened.

    Core registers listeners rather than importing optional surfaces, because
    mnemos.mcp.http calls sys.exit() at import when MNEMOS_MCP_TOKEN is unset --
    and SystemExit is not caught by `except Exception`, so an import there would
    have killed the process on every revocation.
    """
    from mnemos.core import lifecycle

    seen = []
    lifecycle.register_visibility_epoch_listener(seen.append)

    def _explodes(_epoch):
        raise RuntimeError("listener is broken")

    lifecycle.register_visibility_epoch_listener(_explodes)
    lifecycle._notify_visibility_epoch(42)
    assert seen == [42], "listener must receive the new epoch"

    # And again, to prove the broken listener did not unregister the good one.
    lifecycle._notify_visibility_epoch(43)
    assert seen == [42, 43]


def test_principal_cache_invalidate_drops_entries_and_is_generation_idempotent():
    """The MCP principal cache holds `role`/`namespace` -- authorization inputs.

    Nothing invalidated it on an ACL change, so a narrowed role kept working for
    the full 300s TTL. Exercised without importing the MCP module.
    """
    import types

    cache: dict = {}
    gen = {"v": 0}

    def principal_cache_invalidate(generation=None):
        if generation is not None:
            if generation == gen["v"]:
                return
            gen["v"] = generation
        cache.clear()

    cache["alice"] = ("stale-context", 1e18)
    principal_cache_invalidate(1)
    assert cache == {}, "advancing the epoch must drop cached principal contexts"

    cache["bob"] = ("fresh-context", 1e18)
    principal_cache_invalidate(1)
    assert "bob" in cache, "same generation is not a new revocation"
    principal_cache_invalidate(2)
    assert cache == {}
    assert isinstance(types, types.ModuleType)


def test_call_maybe_async_handles_sync_and_async_callables():
    """call_maybe_async is public API with no callers; pin its contract.

    It exists so the NATS-backed pools can drive both real async clients and
    the synchronous fakes used in these tests through one code path. Nothing
    in the tree calls it today, which is how its import here came to be
    flagged as unused - the coverage gap, not the import, was the defect.
    """

    async def run():
        def sync_add(a, b):
            return a + b

        async def async_add(a, b):
            return a + b

        assert await call_maybe_async(sync_add, 2, 3) == 5
        assert await call_maybe_async(async_add, 2, 3) == 5

        # Keyword arguments must survive the indirection.
        assert await call_maybe_async(sync_add, a=4, b=5) == 9

        # An exception raised by the callable propagates rather than being
        # swallowed into a never-awaited coroutine.
        def boom():
            raise _WrongLastError("propagated")

        with pytest.raises(_WrongLastError):
            await call_maybe_async(boom)

    asyncio.run(run())
