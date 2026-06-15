"""Tests for the PANTHEON cooldown circuit-breaker."""

from __future__ import annotations

from mnemos.domain.pantheon.cooldown import (
    DEFAULT_COOLDOWN_SECONDS,
    CooldownManager,
    InMemoryCooldownStore,
    evaluate_cooldown,
)
from mnemos.domain.pantheon.errors import normalize_error

NOW = 1000.0


def _err(status=None, body=None):
    return normalize_error(status_code=status, body=body)


# ── pure evaluate_cooldown ──────────────────────────────────────────────────


def test_plain_400_never_trips():
    d = evaluate_cooldown(_err(400), successes=0, failures=10, is_single_deployment_group=False)
    assert d.should_cooldown is False


def test_api_connection_never_trips():
    d = evaluate_cooldown(
        _err(body="APIConnectionError: refused"), successes=0, failures=10, is_single_deployment_group=False
    )
    assert d.should_cooldown is False


def test_single_deployment_group_never_trips():
    d = evaluate_cooldown(_err(429), successes=0, failures=9, is_single_deployment_group=True)
    assert d.should_cooldown is False


def test_429_trips_multi():
    d = evaluate_cooldown(_err(429), successes=0, failures=1, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert d.reason == "rate_limit"
    assert d.cooldown_seconds == DEFAULT_COOLDOWN_SECONDS


def test_permanent_auth_and_not_found_trip_multi():
    assert evaluate_cooldown(_err(401), successes=0, failures=1, is_single_deployment_group=False).should_cooldown
    assert evaluate_cooldown(_err(404), successes=0, failures=1, is_single_deployment_group=False).should_cooldown


def test_failure_rate_trips_over_threshold():
    # 4 fail / 6 total = 66% > 50%, total >= 5, server errors (cooldownable, not permanent)
    d = evaluate_cooldown(_err(500), successes=2, failures=4, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert d.reason == "failure_rate"


def test_failure_rate_not_tripped_below_min_requests():
    # 3 fail / 3 total = 100% but total < 5
    d = evaluate_cooldown(_err(500), successes=0, failures=3, is_single_deployment_group=False)
    assert d.should_cooldown is False


def test_failure_rate_not_tripped_at_exactly_half():
    # 5 fail / 10 = 50%, needs > 50%
    d = evaluate_cooldown(_err(500), successes=5, failures=5, is_single_deployment_group=False)
    assert d.should_cooldown is False


def test_custom_cooldown_seconds_passthrough():
    d = evaluate_cooldown(_err(429), successes=0, failures=1, is_single_deployment_group=False, cooldown_seconds=30)
    assert d.cooldown_seconds == 30


# ── CooldownManager + store ─────────────────────────────────────────────────


def _mgr():
    return CooldownManager(InMemoryCooldownStore())


def test_manager_trips_and_marks_cooled():
    m = _mgr()
    d = m.record_failure("gpt", _err(429), NOW, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert m.is_cooled("gpt", NOW) is True
    assert m.is_cooled("gpt", NOW + DEFAULT_COOLDOWN_SECONDS + 0.1) is False  # logical-TTL expiry


def test_manager_single_group_does_not_cool():
    m = _mgr()
    d = m.record_failure("only", _err(429), NOW, is_single_deployment_group=True)
    assert d.should_cooldown is False
    assert m.is_cooled("only", NOW) is False


def test_manager_filter_available_removes_cooled():
    m = _mgr()
    m.record_failure("b", _err(429), NOW, is_single_deployment_group=False)
    assert m.filter_available(["a", "b", "c"], NOW) == ["a", "c"]


def test_manager_failure_rate_path():
    m = _mgr()
    # 2 successes, then server-error failures in the same minute. Trips when the
    # failure rate first exceeds 50% with total >= 5:
    #   fail1: 1/3=33%; fail2: 2/4=50% (total<5 anyway); fail3: 3/5=60% -> TRIP.
    m.record_success("x", NOW)
    m.record_success("x", NOW)
    for _ in range(2):
        d = m.record_failure("x", _err(503), NOW, is_single_deployment_group=False)
        assert d.should_cooldown is False
    d = m.record_failure("x", _err(503), NOW, is_single_deployment_group=False)
    assert d.should_cooldown is True
    assert d.reason == "failure_rate"


def test_tenant_isolation():
    m = _mgr()
    m.record_failure("shared", _err(429), NOW, is_single_deployment_group=False, tenant="A")
    assert m.is_cooled("shared", NOW, tenant="A") is True
    assert m.is_cooled("shared", NOW, tenant="B") is False  # tenant B's key unaffected


def test_store_counts_are_minute_bucketed():
    s = InMemoryCooldownStore()
    s.incr("t", "d", 100, success=True)
    s.incr("t", "d", 100, success=False)
    s.incr("t", "d", 101, success=False)
    assert s.get_counts("t", "d", 100) == (1, 1)
    assert s.get_counts("t", "d", 101) == (0, 1)
    assert s.get_counts("t", "d", 999) == (0, 0)


def test_store_prunes_stale_minute_buckets():
    s = InMemoryCooldownStore()
    s.incr("t", "d", 100, success=True)
    s.incr("t", "d", 101, success=False)  # cutoff=100 -> minute 100 kept (not < 100)
    assert s.get_counts("t", "d", 100) == (1, 0)
    s.incr("t", "d", 102, success=False)  # cutoff=101 -> minute 100 pruned (< 101)
    assert s.get_counts("t", "d", 100) == (0, 0)  # pruned
    assert s.get_counts("t", "d", 101) == (0, 1)  # previous minute kept
    assert s.get_counts("t", "d", 102) == (0, 1)


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


def test_nats_cooldown_cross_instance_failure_accumulation_trips():
    from mnemos.domain.pantheon.cooldown_nats import NatsJetStreamCooldownStore

    kv = _FakeNatsKv()
    first = CooldownManager(NatsJetStreamCooldownStore(js=kv))
    second = CooldownManager(NatsJetStreamCooldownStore(js=kv))
    try:
        first.record_success("dep", NOW, tenant="tenant")
        first.record_success("dep", NOW, tenant="tenant")
        for _ in range(2):
            decision = first.record_failure("dep", _err(503), NOW, is_single_deployment_group=False, tenant="tenant")
            assert not decision.should_cooldown
        decision = second.record_failure("dep", _err(503), NOW, is_single_deployment_group=False, tenant="tenant")
        assert decision.should_cooldown
        assert second.is_cooled("dep", NOW, tenant="tenant")
    finally:
        first._store.close()
        second._store.close()


def test_nats_cooldown_peer_seen_on_first_request_and_monotonic():
    from mnemos.domain.pantheon.cooldown_nats import NatsJetStreamCooldownStore

    kv = _FakeNatsKv()
    first = NatsJetStreamCooldownStore(js=kv)
    second = NatsJetStreamCooldownStore(js=kv)
    try:
        first.set_cooldown("tenant", "dep", NOW + 100)
        first.flush()
        assert second.get_cooled_until("tenant", "dep") == NOW + 100
        second.set_cooldown("tenant", "dep", NOW + 5)
        second.flush()
        assert first.get_cooled_until("tenant", "dep") == NOW + 100
    finally:
        first.close()
        second.close()


def test_nats_counter_load_is_idempotent_and_uses_bucket_ttl_only():
    from mnemos.domain.pantheon.cooldown_nats import NatsJetStreamCooldownStore, _counter_key

    kv = _FakeNatsKv()
    store = NatsJetStreamCooldownStore(js=kv)
    try:
        store.incr("tenant", "dep", 16, success=False)
        assert store.refresh_counts("tenant", "dep", 16) == (0, 1)
        assert store.refresh_counts("tenant", "dep", 16) == (0, 1)
        _counter_key("tenant", "dep", 16)  # key construction remains opaque and deterministic
    finally:
        store.close()


def test_nats_cooldown_keys_are_opaque():
    from mnemos.domain.pantheon.cooldown_nats import _cooldown_key, _counter_key

    key = _cooldown_key("namespace:user@example.com", "provider/model")
    counter = _counter_key("namespace:user@example.com", "provider/model", 16)
    for plaintext in ("namespace", "user", "example", "provider", "model"):
        assert plaintext not in key
        assert plaintext not in counter


def test_nats_bucket_created_with_ttl_and_no_per_key_ttl():
    from mnemos.domain.pantheon.cooldown_nats import NatsJetStreamCooldownStore

    class Missing(Exception):
        pass

    class FakeJs:
        def __init__(self):
            self.configs = []
            self.config = None
            self.kv = _FakeNatsKv()

        async def key_value(self, _bucket):
            raise Missing("not found")

        def create_key_value(self, config=None, **kwargs):
            self.config = config or kwargs
            self.configs.append(self.config)
            return self.kv

    js = FakeJs()
    store = NatsJetStreamCooldownStore(js=js, counter_ttl_seconds=7)
    try:
        store.flush()
        ttls = [cfg.get("ttl") if isinstance(cfg, dict) else getattr(cfg, "ttl", None) for cfg in js.configs]
        assert 7 in ttls
        assert 86400 in ttls
        store.incr("tenant", "dep", 17, success=False)
        assert store.refresh_counts("tenant", "dep", 17) == (0, 1)
    finally:
        store.close()


def test_router_runtime_uses_async_nats_methods_without_sync_result(monkeypatch):
    from mnemos.domain.pantheon.cooldown_nats import NatsJetStreamCooldownStore
    from mnemos.domain.pantheon.runtime import RouterRuntime

    store = NatsJetStreamCooldownStore(js=_FakeNatsKv())
    monkeypatch.setattr(store, "_run_sync", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sync path")))
    runtime = RouterRuntime(CooldownManager(store), clock=lambda: NOW, sleep=lambda _delay: None)  # type: ignore[arg-type]

    async def call(_deployment):
        return "ok"

    try:
        result = __import__("asyncio").run(runtime.route(["dep"], call, classify=lambda exc: _err(500)))
        assert result.result == "ok"
    finally:
        store.close()
