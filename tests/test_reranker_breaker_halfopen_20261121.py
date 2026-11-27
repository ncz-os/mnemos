"""Regression tests for the reranker's half-open circuit breaker.

The reranker breaker mirrors ``mnemos/runtime/embedder.py::_HttpBackend``
(see its module docstring): after the cooldown elapses, exactly one
call must probe, all others stay blocked until that probe resolves,
and a failed probe reopens with a fresh cooldown.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from mnemos.domain.search.reranker import Reranker


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return {"results": [{"index": 0, "relevance_score": 0.87}]}


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
def breaker(monkeypatch: pytest.MonkeyPatch) -> tuple[Reranker, FakeClock]:
    clock = FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    reranker = Reranker(url="http://rerank.test", model="fake-model", timeout=1.0, cb_threshold=2, cb_cooldown=10.0)
    return reranker, clock


@pytest.mark.asyncio
async def test_only_one_concurrent_caller_probes_half_open(breaker: tuple[Reranker, FakeClock]) -> None:
    reranker, clock = breaker
    client = FakeClient(FakeResponse())
    reranker._client = client
    reranker._record_failure()
    reranker._record_failure()
    clock.now += 10.0

    calls = [asyncio.create_task(reranker.rerank("q", ["doc"])) for _ in range(8)]
    await client.started.wait()
    await asyncio.sleep(0)
    assert client.calls == 1
    assert reranker._breaker_opened_at is not None
    client.release.set()
    results = await asyncio.gather(*calls)

    assert results.count([0.87]) == 1
    assert client.calls == 1
    assert reranker._breaker_open() is False


def test_probe_failure_reopens_immediately_with_fresh_cooldown(
    breaker: tuple[Reranker, FakeClock],
) -> None:
    reranker, clock = breaker
    reranker._record_failure()
    reranker._record_failure()
    clock.now += 10.0

    assert reranker._breaker_open() is False
    assert reranker._breaker_open() is True
    reranker._record_failure()

    assert reranker._breaker_opened_at == clock.now
    assert reranker._breaker_open() is True
    clock.now += 9.9
    assert reranker._breaker_open() is True
    clock.now += 0.1
    assert reranker._breaker_open() is False


@pytest.mark.asyncio
async def test_probe_success_closes_breaker(breaker: tuple[Reranker, FakeClock]) -> None:
    reranker, clock = breaker
    client = FakeClient(FakeResponse())
    client.release.set()
    reranker._client = client
    reranker._record_failure()
    reranker._record_failure()
    clock.now += 10.0

    assert await reranker.rerank("q", ["doc"]) == [0.87]
    assert reranker._breaker_opened_at is None
    assert reranker._consecutive_failures == 0
    assert reranker._breaker_open() is False


def test_closed_breaker_failure_threshold_is_unchanged(breaker: tuple[Reranker, FakeClock]) -> None:
    reranker, clock = breaker

    assert reranker._breaker_open() is False
    reranker._record_failure()
    assert reranker._consecutive_failures == 1
    assert reranker._breaker_opened_at is None
    reranker._record_failure()

    assert reranker._consecutive_failures == 2
    assert reranker._breaker_opened_at == clock.now
    assert reranker._breaker_open() is True


def test_breaker_lock_prevents_double_probe_claim_from_threads(
    breaker: tuple[Reranker, FakeClock],
) -> None:
    """The half-open claim must be atomic under real thread contention.

    After the cooldown elapses, a flood of concurrent callers must produce
    exactly one probe claim (``_breaker_open()`` returning False) and block
    every other caller; the breaker remains open (probe in flight) for the
    rest.
    """
    import threading

    reranker, clock = breaker
    reranker._record_failure()
    reranker._record_failure()
    clock.now += 10.0

    results: list[bool] = []
    results_lock = threading.Lock()

    def check() -> None:
        open_state = reranker._breaker_open()
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
    assert reranker._breaker_open() is True
