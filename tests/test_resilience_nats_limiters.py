from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from types import SimpleNamespace

import pytest

import mnemos.core.resilience as resilience


class WrongLastError(Exception):
    """Named and worded like nats-py's revision-conflict exception."""


class KeyNotFoundError(Exception):
    """Named and worded like nats-py's missing-key exception."""


class FakeEntry:
    def __init__(self, value: bytes, revision: int) -> None:
        self.value = value
        self.revision = revision


class FakeNatsKv:
    """Thread-safe JetStream KV fake with real create/update CAS semantics."""

    def __init__(self, *, fail_updates: int = 0) -> None:
        self._values: dict[str, tuple[bytes, int]] = {}
        self._revision = 0
        self._lock = threading.Lock()
        self.fail_updates = fail_updates
        self.conflicts = 0

    async def get(self, key: str) -> FakeEntry:
        self._validate_key(key)
        await asyncio.sleep(0)
        with self._lock:
            try:
                value, revision = self._values[key]
            except KeyError:
                raise KeyNotFoundError("key not found") from None
        return FakeEntry(value, revision)

    async def create(self, key: str, value: bytes) -> int:
        self._validate_key(key)
        await asyncio.sleep(0)
        with self._lock:
            if key in self._values:
                self.conflicts += 1
                raise WrongLastError("wrong last sequence")
            return self._write(key, value)

    async def put(self, key: str, value: bytes) -> int:
        self._validate_key(key)
        await asyncio.sleep(0)
        with self._lock:
            return self._write(key, value)

    async def update(self, key: str, value: bytes, *, last: int) -> int:
        self._validate_key(key)
        await asyncio.sleep(0)
        with self._lock:
            if self.fail_updates:
                self.fail_updates -= 1
                self.conflicts += 1
                old_value, _old_revision = self._values[key]
                self._write(key, old_value)
                raise WrongLastError("wrong last sequence")
            try:
                _old_value, revision = self._values[key]
            except KeyError:
                self.conflicts += 1
                raise WrongLastError("wrong last sequence") from None
            if revision != last:
                self.conflicts += 1
                raise WrongLastError("wrong last sequence")
            return self._write(key, value)

    def seed(self, key: str, payload: dict[str, object]) -> None:
        self._validate_key(key)
        value = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        with self._lock:
            self._write(key, value)

    def payload(self, key: str) -> dict[str, object]:
        self._validate_key(key)
        with self._lock:
            value, _revision = self._values[key]
        return json.loads(value)

    def _write(self, key: str, value: bytes) -> int:
        self._revision += 1
        self._values[key] = (value, self._revision)
        return self._revision

    @staticmethod
    def _validate_key(key: str) -> None:
        if re.fullmatch(r"[-/_=.A-Za-z0-9]+", key) is None:
            raise ValueError(f"invalid JetStream KV key: {key}")


class FakeJetStream:
    """Creates named KV buckets and records their bucket-wide TTLs."""

    def __init__(self) -> None:
        self.buckets: dict[str, FakeNatsKv] = {}
        self.ttls: dict[str, int | float | None] = {}
        self._lock = threading.Lock()

    async def key_value(self, bucket: str) -> FakeNatsKv:
        with self._lock:
            try:
                return self.buckets[bucket]
            except KeyError:
                raise KeyNotFoundError("bucket not found") from None

    async def create_key_value(
        self,
        bucket: str | None = None,
        *,
        config: object | None = None,
        ttl: int | float | None = None,
    ) -> FakeNatsKv:
        if config is not None:
            bucket = str(getattr(config, "bucket"))
            ttl = getattr(config, "ttl", None)
        if bucket is None:
            raise AssertionError("bucket name is required")
        with self._lock:
            kv = self.buckets.setdefault(bucket, FakeNatsKv())
            self.ttls.setdefault(bucket, ttl)
            return kv


def _nats_settings() -> SimpleNamespace:
    return SimpleNamespace(nats=SimpleNamespace(url="nats://unreachable.invalid:4222", token=None))


def _run_in_parallel_threads(call, count: int) -> list[bool]:
    barrier = threading.Barrier(count)
    results = [False] * count
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait()
            results[index] = bool(asyncio.run(call()))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    return results


def test_nats_rate_limiter_slides_and_prunes_boundary_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        kv = FakeNatsKv()
        kv.seed("rate.openai", {"timestamps": [940.0, 970.0]})
        monkeypatch.setattr(resilience.time, "time", lambda: 1000.0)
        pool = resilience.NatsRateLimiterPool(kv, "rate.", overrides={"openai": 2})
        try:
            assert await pool.is_allowed("openai")
            assert not await pool.is_allowed("openai")
            assert kv.payload("rate.openai") == {"timestamps": [970.0, 1000.0]}
        finally:
            pool.close()

    asyncio.run(run())


def test_nats_rate_limiter_retries_revision_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        kv = FakeNatsKv(fail_updates=1)
        kv.seed("rate.openai", {"timestamps": [999.0]})
        monkeypatch.setattr(resilience.time, "time", lambda: 1000.0)
        pool = resilience.NatsRateLimiterPool(kv, "rate.", overrides={"openai": 3})
        try:
            assert await pool.acquire("openai")
            assert kv.conflicts == 1
            assert kv.payload("rate.openai") == {"timestamps": [999.0, 1000.0]}
        finally:
            pool.close()

    asyncio.run(run())


def test_nats_rate_limiter_concurrent_callers_do_not_over_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        kv = FakeNatsKv()
        monkeypatch.setattr(resilience.time, "time", lambda: 1000.0)
        pools = [
            resilience.NatsRateLimiterPool(kv, "rate.", overrides={"openai": 3})
            for _index in range(6)
        ]
        try:
            results = await asyncio.gather(*(pool.acquire("openai") for pool in pools))
            assert sum(results) == 3
            assert len(kv.payload("rate.openai")["timestamps"]) == 3
        finally:
            for pool in pools:
                pool.close()

    asyncio.run(run())


def test_nats_concurrency_limiter_prunes_expired_leases_and_retries_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        kv = FakeNatsKv(fail_updates=1)
        kv.seed("conc.openai", {"leases": {"dead": 999.0, "live": 1100.0}})
        monkeypatch.setattr(resilience.time, "time", lambda: 1000.0)
        pool = resilience.NatsConcurrencyLimiterPool(kv, "conc.", overrides={"openai": 2})
        try:
            assert await pool.acquire("openai")
            leases = kv.payload("conc.openai")["leases"]
            assert "dead" not in leases
            assert "live" in leases
            assert len(leases) == 2
            assert kv.conflicts == 1
            assert not await pool.acquire("openai")
            await pool.release("openai")
            assert kv.payload("conc.openai") == {"leases": {"live": 1100.0}}
        finally:
            pool.close()

    asyncio.run(run())


def test_nats_concurrency_limiter_concurrent_callers_do_not_over_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        kv = FakeNatsKv()
        monkeypatch.setattr(resilience.time, "time", lambda: 1000.0)
        pools = [
            resilience.NatsConcurrencyLimiterPool(kv, "conc.", overrides={"openai": 2})
            for _index in range(5)
        ]
        try:
            results = await asyncio.gather(*(pool.acquire("openai") for pool in pools))
            assert sum(results) == 2
            assert len(kv.payload("conc.openai")["leases"]) == 2
            for pool, acquired in zip(pools, results, strict=True):
                if acquired:
                    await pool.release("openai")
            assert kv.payload("conc.openai") == {"leases": {}}
        finally:
            for pool in pools:
                pool.close()

    asyncio.run(run())


def test_nats_concurrency_late_heartbeat_cannot_resurrect_expired_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        now = {"value": 1000.0}
        monkeypatch.setattr(resilience.time, "time", lambda: now["value"])
        kv = FakeNatsKv()
        first = resilience.NatsConcurrencyLimiterPool(
            kv,
            "conc.",
            overrides={"openai": 1},
            lease_seconds=10,
        )
        second = resilience.NatsConcurrencyLimiterPool(
            kv,
            "conc.",
            overrides={"openai": 1},
            lease_seconds=10,
        )
        try:
            assert await first.acquire("openai")
            first_token = first._tokens["openai"][0]
            now["value"] = 1011.0
            assert await second.acquire("openai")
            second_token = second._tokens["openai"][0]

            renewed = await first._limiter._refresh_slot_record("openai", first_token, 1)
            assert not renewed
            assert kv.payload("conc.openai") == {
                "leases": {second_token: 1021.0},
            }
            await first.release("openai")
            await second.release("openai")
        finally:
            first.close()
            second.close()

    asyncio.run(run())


def test_nats_concurrency_reserve_releases_its_exact_token_out_of_order() -> None:
    async def run() -> None:
        kv = FakeNatsKv()
        pool = resilience.NatsConcurrencyLimiterPool(kv, "conc.", overrides={"openai": 2})
        first = pool.reserve("openai")
        second = pool.reserve("openai")
        try:
            assert await first.__aenter__()
            first_token = pool._tokens["openai"][0]
            assert await second.__aenter__()
            second_token = pool._tokens["openai"][1]

            await first.__aexit__(None, None, None)
            leases = kv.payload("conc.openai")["leases"]
            assert first_token not in leases
            assert second_token in leases
            assert second_token in pool._limiter._heartbeats

            await second.__aexit__(None, None, None)
            assert kv.payload("conc.openai") == {"leases": {}}
        finally:
            pool.close()

    asyncio.run(run())


def test_nats_limiters_use_valid_defaults_and_separate_bucket_ttls() -> None:
    async def run() -> None:
        jetstream = FakeJetStream()
        rate = resilience.NatsRateLimiterPool(jetstream, overrides={"openai": 1})
        concurrency = resilience.NatsConcurrencyLimiterPool(
            jetstream,
            overrides={"openai": 1},
            lease_seconds=17,
        )
        try:
            assert await rate.acquire("openai")
            assert await concurrency.acquire("openai")
            await concurrency.release("openai")

            assert set(jetstream.buckets) == {
                "MNEMOS_GRAEAE_RATE_LIMIT",
                "MNEMOS_GRAEAE_CONCURRENCY",
            }
            assert jetstream.ttls["MNEMOS_GRAEAE_RATE_LIMIT"] == 120
            assert jetstream.ttls["MNEMOS_GRAEAE_CONCURRENCY"] == 17
        finally:
            rate.close()
            concurrency.close()

    asyncio.run(run())


def test_nats_limiters_use_local_fallback_when_nats_py_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        monkeypatch.setitem(sys.modules, "nats", None)
        rate = resilience.NatsRateLimiterPool(
            None,
            "rate.",
            overrides={"openai": 1},
            settings=_nats_settings(),
        )
        concurrency = resilience.NatsConcurrencyLimiterPool(
            None,
            "conc.",
            overrides={"openai": 1},
            settings=_nats_settings(),
        )
        try:
            assert await rate.acquire("openai")
            assert not await rate.acquire("openai")
            assert concurrency.is_available("openai")
            assert await concurrency.acquire("openai")
            assert not await concurrency.acquire("openai")
            await concurrency.release("openai")
            assert await concurrency.acquire("openai")
            await concurrency.release("openai")
        finally:
            rate.close()
            concurrency.close()

    asyncio.run(run())


def test_nats_local_fallback_limits_across_caller_threads_and_event_loops() -> None:
    rate = resilience.NatsRateLimiterPool(None, "rate.", overrides={"openai": 1})
    concurrency = resilience.NatsConcurrencyLimiterPool(None, "conc.", overrides={"openai": 1})
    try:
        rate_results = _run_in_parallel_threads(lambda: rate.acquire("openai"), 2)
        concurrency_results = _run_in_parallel_threads(lambda: concurrency.acquire("openai"), 2)

        assert sum(rate_results) == 1
        assert sum(concurrency_results) == 1
        asyncio.run(concurrency.release("openai"))
        assert asyncio.run(concurrency.acquire("openai"))
        asyncio.run(concurrency.release("openai"))
    finally:
        rate.close()
        concurrency.close()
