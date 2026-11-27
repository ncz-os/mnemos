"""Regression tests for the HTTP embedder's half-open circuit breaker."""

from __future__ import annotations

import asyncio
import time

import pytest

from mnemos.runtime.embedder import _HttpBackend


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return {"data": [{"embedding": [0.1, 0.2]}]}


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return self.response


@pytest.fixture
def breaker(monkeypatch: pytest.MonkeyPatch) -> tuple[_HttpBackend, FakeClock]:
    clock = FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    embedder = _HttpBackend("http://embed.test", "model", 1.0, 100, cb_threshold=2, cb_cooldown=10.0)
    return embedder, clock


@pytest.mark.asyncio
async def test_only_one_concurrent_caller_probes_half_open(breaker: tuple[_HttpBackend, FakeClock]) -> None:
    embedder, clock = breaker
    client = FakeClient(FakeResponse())
    embedder._client = client
    embedder._record_failure()
    embedder._record_failure()
    clock.now += 10.0

    calls = [asyncio.create_task(embedder.embed_async(f"request-{i}")) for i in range(8)]
    await client.started.wait()
    await asyncio.sleep(0)
    assert client.calls == 1
    assert embedder._breaker_opened_at is not None
    client.release.set()
    results = await asyncio.gather(*calls)

    assert results.count([0.1, 0.2]) == 1
    assert client.calls == 1
    assert embedder._breaker_open() is False


def test_probe_failure_reopens_immediately_with_fresh_cooldown(
    breaker: tuple[_HttpBackend, FakeClock],
) -> None:
    embedder, clock = breaker
    embedder._record_failure()
    embedder._record_failure()
    clock.now += 10.0

    assert embedder._breaker_open() is False
    assert embedder._breaker_open() is True
    embedder._record_failure()

    assert embedder._breaker_opened_at == clock.now
    assert embedder._breaker_open() is True
    clock.now += 9.9
    assert embedder._breaker_open() is True
    clock.now += 0.1
    assert embedder._breaker_open() is False


@pytest.mark.asyncio
async def test_probe_success_closes_breaker(breaker: tuple[_HttpBackend, FakeClock]) -> None:
    embedder, clock = breaker
    client = FakeClient(FakeResponse())
    client.release.set()
    embedder._client = client
    embedder._record_failure()
    embedder._record_failure()
    clock.now += 10.0

    assert await embedder.embed_async("probe") == [0.1, 0.2]
    assert embedder._breaker_opened_at is None
    assert embedder._consecutive_failures == 0
    assert embedder._breaker_open() is False


def test_closed_breaker_failure_threshold_is_unchanged(breaker: tuple[_HttpBackend, FakeClock]) -> None:
    embedder, clock = breaker

    assert embedder._breaker_open() is False
    embedder._record_failure()
    assert embedder._consecutive_failures == 1
    assert embedder._breaker_opened_at is None
    embedder._record_failure()

    assert embedder._consecutive_failures == 2
    assert embedder._breaker_opened_at == clock.now
    assert embedder._breaker_open() is True


def test_breaker_lock_prevents_double_probe_claim_from_threads(
    breaker: tuple[_HttpBackend, FakeClock],
) -> None:
    """The half-open claim must be atomic under real thread contention.

    After the cooldown elapses, a flood of concurrent callers must produce
    exactly one probe claim (``_breaker_open()`` returning False) and block
    every other caller; the breaker remains open (probe in flight) for the
    rest.
    """
    import threading

    embedder, clock = breaker
    embedder._record_failure()
    embedder._record_failure()
    clock.now += 10.0

    results: list[bool] = []
    results_lock = threading.Lock()

    def check() -> None:
        open_state = embedder._breaker_open()
        with results_lock:
            results.append(open_state)

    threads = [threading.Thread(target=check) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one caller claimed the probe slot; the other 49 were blocked.
    assert results.count(False) == 1
    assert results.count(True) == 49
    # The probe is in flight, so the breaker is still open for everyone else.
    assert embedder._breaker_open() is True
