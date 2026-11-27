"""Characterisation of ``mnemos/webhooks/workers.py`` (FB1130).

Pins the three webhook lifespan loops (repair / delivery / recovery) and the
recovery claim helper: their cadence, their phase choice, their exception and
cancellation semantics, and the exact claim call each backend arm makes. No
database is involved — the loops are driven by a scripted clock and a fake
sleep, and the ABC claim is a recording double. Behaviour that looks wrong is
pinned and named as a divergence in the summary.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest

import mnemos.core.lifecycle as lifecycle
from mnemos.webhooks import sender, workers
from mnemos.webhooks import types as webhook_types

_REAL_SLEEP = asyncio.sleep


# ── Loop-driving fakes ─────────────────────────────────────────────────────


class _Clock:
    """Scripted loop clock: each time() call consumes the next value."""

    def __init__(self, *values: float) -> None:
        self._values = list(values) or [0.0]
        self._calls = 0

    def time(self) -> float:
        value = self._values[min(self._calls, len(self._values) - 1)]
        self._calls += 1
        return value


def _shim(monkeypatch: pytest.MonkeyPatch, sleeps: list, *, stop_after: int, clock=None):
    """Replace the module's ``asyncio`` with a fake sleep + clock."""

    class _Shim:
        CancelledError = asyncio.CancelledError

        @staticmethod
        def get_running_loop():
            return SimpleNamespace(time=clock.time if clock is not None else lambda: 0.0)

        @staticmethod
        async def sleep(delay):
            sleeps.append(delay)
            await _REAL_SLEEP(0)
            if len(sleeps) >= stop_after:
                raise asyncio.CancelledError

    monkeypatch.setattr(workers, "asyncio", _Shim)
    return _Shim


def _constants(**overrides):
    base = {
        "REPAIR_BURST_SECONDS": 10.0,
        "REPAIR_BURST_INTERVAL": 2.0,
        "REPAIR_PERIODIC_INTERVAL": 99.0,
        "RECOVERY_POLL_INTERVAL": 7.0,
        "WEBHOOK_LEASE_SECONDS": 30,
        "MAX_ATTEMPTS": 4,
        "NEW_CODE_WRITER_REVISION": 3,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ── _semaphore_available ───────────────────────────────────────────────────


def test_semaphore_headroom_is_the_free_slot_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workers, "_get_send_semaphore", lambda: SimpleNamespace(_value=7))

    assert workers._semaphore_available() == 7


def test_a_negative_slot_count_is_clamped_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # asyncio.Semaphore._value can over-release; the batch size must not go
    # negative either way.
    monkeypatch.setattr(workers, "_get_send_semaphore", lambda: SimpleNamespace(_value=-3))

    assert workers._semaphore_available() == 0


# ── _recover_due_deliveries: the ABC arm ───────────────────────────────────


def _abc_claim(delivery_id: str = "11111111-1111-1111-1111-111111111111"):
    delivery = SimpleNamespace(
        id=delivery_id,
        url="https://example.test/hook",
        secret="s3cret",
        revoked=False,
        owner_id="owner",
        namespace="default",
    )
    return SimpleNamespace(
        delivery=delivery,
        lease_expires_at="lease-expiry",
        claim_db_now="db-now",
    )


class _FakeBackend:
    def __init__(self, claimed, tx_marker: object) -> None:
        self.claimed = claimed
        self.tx_marker = tx_marker
        self.calls: list = []
        self.webhooks = SimpleNamespace(claim_due_deliveries=self._claim)
        self.transactional_calls = 0

    def _transactional(self):
        backend = self

        class _Ctx:
            async def __aenter__(self):
                backend.transactional_calls += 1
                return backend.tx_marker

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    transactional = _transactional

    async def _claim(self, tx, **kwargs):
        self.calls.append((tx, kwargs))
        return self.claimed


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {
        "scheduled": [],
        "attempts": [],
        "sleeps": [],
        "legacy_pool_reads": 0,
    }

    def _schedule(obj):
        state["scheduled"].append(obj)
        return obj

    def _attempt(delivery_id, **kwargs):
        state["attempts"].append((delivery_id, kwargs))
        return ("attempt", delivery_id, kwargs)

    monkeypatch.setattr(lifecycle, "_schedule_delivery_attempt", _schedule, raising=False)
    monkeypatch.setattr(sender, "_attempt_delivery", _attempt)
    monkeypatch.setattr(workers, "webhook_types", _constants())
    monkeypatch.setattr(workers, "_get_send_semaphore", lambda: SimpleNamespace(_value=50))

    def _legacy_pool():
        state["legacy_pool_reads"] += 1
        return None

    monkeypatch.setattr(lifecycle, "get_temporary_legacy_webhook_pool", _legacy_pool, raising=False)
    return state


@pytest.mark.asyncio
async def test_the_abc_claim_receives_the_documented_arguments(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    claim = _abc_claim()
    backend = _FakeBackend([claim], tx_marker=object())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend, raising=False)
    monkeypatch.setattr(workers.asyncio, "sleep", _noop, raising=False)

    recovered = await workers._recover_due_deliveries(limit=10)

    assert recovered == 1
    assert backend.transactional_calls == 1
    tx, kwargs = backend.calls[0]
    assert tx is backend.tx_marker
    assert kwargs["limit"] == 10
    assert kwargs["lease_seconds"] == 30
    assert kwargs["max_attempts"] == 4
    assert kwargs["writer_revision"] == 3
    uuid.UUID(kwargs["lease_token"])  # a real UUID string, not a placeholder


@pytest.mark.asyncio
async def test_each_abc_claim_is_wrapped_and_scheduled_once(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _abc_claim("11111111-1111-1111-1111-111111111111")
    second = _abc_claim("22222222-2222-2222-2222-222222222222")
    backend = _FakeBackend([first, second], tx_marker=object())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend, raising=False)
    monkeypatch.setattr(workers.asyncio, "sleep", _noop, raising=False)

    recovered = await workers._recover_due_deliveries()

    assert recovered == 2
    ids = [delivery_id for delivery_id, _kwargs in wired["attempts"]]
    assert ids == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert len(wired["scheduled"]) == 2
    for _delivery_id, kwargs in wired["attempts"]:
        wrapped = kwargs["claimed"]
        assert type(wrapped) is workers._ClaimedDelivery
        assert wrapped.lease_token == backend.calls[0][1]["lease_token"]
        # the lease clock values ride on the view, not on the record
        assert wrapped.delivery.lease_expires_at == "lease-expiry"
        assert wrapped.delivery.claim_db_now == "db-now"
        # and the sender's subscript access still resolves
        assert wrapped.delivery["url"] == "https://example.test/hook"


@pytest.mark.asyncio
async def test_the_batch_is_capped_at_fifty_whatever_the_caller_asks(
    wired: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FakeBackend([], tx_marker=object())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend, raising=False)

    await workers._recover_due_deliveries(limit=500)

    assert backend.calls[0][1]["limit"] == 50


@pytest.mark.asyncio
async def test_the_semaphore_sets_the_batch_when_it_is_tighter(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend([], tx_marker=object())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend, raising=False)
    monkeypatch.setattr(workers, "_get_send_semaphore", lambda: SimpleNamespace(_value=3))

    await workers._recover_due_deliveries(limit=50)

    assert backend.calls[0][1]["limit"] == 3


@pytest.mark.asyncio
async def test_a_full_semaphore_skips_the_claim_entirely(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend([], tx_marker=object())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend, raising=False)
    monkeypatch.setattr(workers, "_get_send_semaphore", lambda: SimpleNamespace(_value=0))

    assert await workers._recover_due_deliveries() == 0
    assert backend.calls == []
    assert backend.transactional_calls == 0
    assert wired["scheduled"] == []


@pytest.mark.asyncio
async def test_no_claims_means_no_yield(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend([], tx_marker=object())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend, raising=False)
    sleeps: list = []
    monkeypatch.setattr(workers.asyncio, "sleep", _recorder(sleeps), raising=False)

    await workers._recover_due_deliveries()

    assert sleeps == []


@pytest.mark.asyncio
async def test_a_non_empty_batch_yields_to_the_loop(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend([_abc_claim()], tx_marker=object())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend, raising=False)
    sleeps: list = []
    monkeypatch.setattr(workers.asyncio, "sleep", _recorder(sleeps), raising=False)

    await workers._recover_due_deliveries()

    assert sleeps == [0]


# ── _recover_due_deliveries: the legacy pool arm ───────────────────────────


class _FakeConn:
    def __init__(self) -> None:
        self.transactions = 0

    def transaction(self):
        conn = self

        class _Ctx:
            async def __aenter__(self):
                conn.transactions += 1
                return self

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()
        self.acquires = 0

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                pool.acquires += 1
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_no_backend_and_no_pool_is_a_quiet_no_op(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "_persistence_backend", None, raising=False)

    assert await workers._recover_due_deliveries() == 0
    assert wired["legacy_pool_reads"] == 1
    assert wired["scheduled"] == []


@pytest.mark.asyncio
async def test_the_legacy_pool_claim_schedules_each_row(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "_persistence_backend", None, raising=False)
    pool = _FakePool()
    claims = [
        SimpleNamespace(delivery={"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}),
        SimpleNamespace(delivery={"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}),
    ]
    seen: list = []

    async def _claim(conn, *, limit):
        seen.append((conn, limit))
        return claims

    monkeypatch.setattr(workers, "_claim_recoverable_deliveries", _claim)
    sleeps: list = []
    monkeypatch.setattr(workers.asyncio, "sleep", _recorder(sleeps), raising=False)

    recovered = await workers._recover_due_deliveries(pool, limit=2)

    assert recovered == 2
    assert pool.acquires == 1
    assert pool.conn.transactions == 1
    assert seen == [(pool.conn, 2)]
    assert [delivery_id for delivery_id, _kwargs in wired["attempts"]] == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    assert all(kwargs["pool"] is pool for _delivery_id, kwargs in wired["attempts"])
    assert sleeps == [0]


@pytest.mark.asyncio
async def test_the_legacy_arm_prefers_the_pool_argument_over_the_accessor(
    wired: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "_persistence_backend", None, raising=False)
    pool = _FakePool()

    async def _claim(conn, *, limit):
        return []

    monkeypatch.setattr(workers, "_claim_recoverable_deliveries", _claim)

    await workers._recover_due_deliveries(pool)

    assert pool.acquires == 1
    assert wired["legacy_pool_reads"] == 0


# ── _claim_recoverable_deliveries / _recoverable_delivery_ids ──────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1])
async def test_a_non_positive_legacy_limit_never_queries(wired: dict, limit: int) -> None:
    class _Conn:
        async def fetch(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("fetch must not be called")

    assert await workers._claim_recoverable_deliveries(_Conn(), limit=limit) == []


@pytest.mark.asyncio
async def test_the_legacy_claim_uses_the_documented_bind_order(wired: dict) -> None:
    captured: dict = {}

    class _Conn:
        async def fetch(self, sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return [{"id": "row-1"}, {"id": "row-2"}]

    rows = await workers._claim_recoverable_deliveries(_Conn(), limit=7)

    params = captured["params"]
    assert len(params) == 5
    # The helper reads these off its module import, so the shim values apply.
    assert params[0] == workers.webhook_types.MAX_ATTEMPTS
    uuid.UUID(params[1])  # lease token
    assert params[2] == 7
    assert params[3] == workers.webhook_types.WEBHOOK_LEASE_SECONDS
    assert params[4] == workers.webhook_types.NEW_CODE_WRITER_REVISION
    assert "FOR UPDATE SKIP LOCKED" in captured["sql"]
    assert [type(row) for row in rows] == [workers._ClaimedDelivery] * 2
    assert rows[0].lease_token == rows[1].lease_token == params[1]
    assert rows[0].pre_claim_monotonic == rows[1].pre_claim_monotonic


@pytest.mark.asyncio
async def test_the_legacy_claim_honours_an_explicit_lease(wired: dict) -> None:
    captured: dict = {}

    class _Conn:
        async def fetch(self, sql, *params):
            captured["params"] = params
            return []

    await workers._claim_recoverable_deliveries(_Conn(), limit=1, lease_seconds=12)

    assert captured["params"][3] == 12


@pytest.mark.asyncio
async def test_the_diagnostic_peek_returns_the_driver_result_verbatim(wired: dict) -> None:
    marker = object()

    class _Conn:
        async def fetch(self, sql, *params):
            self.sql = sql
            self.params = params
            return marker

    conn = _Conn()
    result = await workers._recoverable_delivery_ids(conn, limit=9)

    assert result is marker
    assert conn.params == (
        workers.webhook_types.MAX_ATTEMPTS,
        9,
        workers.webhook_types.NEW_CODE_WRITER_REVISION,
    )


# ── delivery_worker_loop / recovery_worker_loop ────────────────────────────


def _recorder(sleeps: list):
    async def _sleep(delay):
        sleeps.append(delay)
        await _REAL_SLEEP(0)

    return _sleep


@pytest.mark.asyncio
async def test_the_delivery_loop_recovers_then_sleeps_the_poll_interval(
    wired: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []

    async def _recover(pool):
        calls.append(pool)
        return 0

    monkeypatch.setattr(workers, "_recover_due_deliveries", _recover)
    sleeps: list = []
    _shim(monkeypatch, sleeps, stop_after=1)

    with pytest.raises(asyncio.CancelledError):
        await workers.delivery_worker_loop("pool-handle")

    assert calls == ["pool-handle"]
    assert sleeps == [7.0]


@pytest.mark.asyncio
async def test_the_delivery_loop_defaults_its_handle_to_none(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    async def _recover(pool):
        calls.append(pool)

    monkeypatch.setattr(workers, "_recover_due_deliveries", _recover)
    sleeps: list = []
    _shim(monkeypatch, sleeps, stop_after=1)

    with pytest.raises(asyncio.CancelledError):
        await workers.delivery_worker_loop()

    assert calls == [None]


@pytest.mark.asyncio
async def test_a_delivery_iteration_failure_is_logged_and_retried(
    wired: dict, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list = []

    async def _recover(pool):
        calls.append(pool)
        raise RuntimeError("db down")

    monkeypatch.setattr(workers, "_recover_due_deliveries", _recover)
    sleeps: list = []
    _shim(monkeypatch, sleeps, stop_after=2)
    caplog.set_level(logging.ERROR)

    with pytest.raises(asyncio.CancelledError):
        await workers.delivery_worker_loop()

    assert len(calls) == 2  # the loop kept going after the failure
    assert sleeps == [7.0, 7.0]
    assert any("webhook delivery recovery worker iteration failed" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_cancellation_of_the_delivery_loop_skips_the_sleep(
    wired: dict, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _recover(pool):
        raise asyncio.CancelledError

    monkeypatch.setattr(workers, "_recover_due_deliveries", _recover)
    sleeps: list = []
    _shim(monkeypatch, sleeps, stop_after=5)
    caplog.set_level(logging.INFO)

    with pytest.raises(asyncio.CancelledError):
        await workers.delivery_worker_loop()

    assert sleeps == []
    assert any("webhook delivery recovery worker cancelled" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_the_recovery_loop_is_the_delivery_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []

    async def _delivery(pool=None):
        calls.append(pool)

    monkeypatch.setattr(workers, "delivery_worker_loop", _delivery)

    assert await workers.recovery_worker_loop("p") is None
    assert calls == ["p"]


# ── repair_worker_loop ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_first_repair_runs_in_the_burst_phase_with_the_burst_interval(
    wired: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []

    async def _repair(handle, *, phase):
        calls.append((handle, phase))

    monkeypatch.setattr(workers.webhook_repair, "_repair_superseded_retrying_deliveries_safely", _repair)
    sleeps: list = []
    _shim(monkeypatch, sleeps, stop_after=1, clock=_Clock(0.0, 0.0, 0.0, 0.0))

    with pytest.raises(asyncio.CancelledError):
        await workers.repair_worker_loop("pool-handle")

    assert calls == [("pool-handle", "burst")]
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_after_the_burst_window_the_repair_goes_periodic(wired: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    async def _repair(handle, *, phase):
        calls.append(phase)

    monkeypatch.setattr(workers.webhook_repair, "_repair_superseded_retrying_deliveries_safely", _repair)
    sleeps: list = []
    # clock: deadline=0+10, next=0, now=0 (burst), sleep arg=0;
    #        now=12 (>= next and past the 10s burst), sleep arg=12
    clock = _Clock(0.0, 0.0, 0.0, 0.0, 12.0, 12.0)
    _shim(monkeypatch, sleeps, stop_after=2, clock=clock)

    with pytest.raises(asyncio.CancelledError):
        await workers.repair_worker_loop(None)

    assert calls == ["burst", "periodic"]
    assert sleeps == [2.0, 99.0]


@pytest.mark.asyncio
async def test_a_repair_iteration_failure_retries_on_the_burst_interval(
    wired: dict, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list = []

    async def _repair(handle, *, phase):
        calls.append(phase)
        raise RuntimeError("repair failed")

    monkeypatch.setattr(workers.webhook_repair, "_repair_superseded_retrying_deliveries_safely", _repair)
    sleeps: list = []
    _shim(monkeypatch, sleeps, stop_after=2, clock=_Clock(0.0, 0.0, 0.0, 0.0))
    caplog.set_level(logging.ERROR)

    with pytest.raises(asyncio.CancelledError):
        await workers.repair_worker_loop(None)

    assert len(calls) == 2
    assert sleeps == [2.0, 2.0]
    assert any("webhook retry repair worker iteration failed" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_repair_cancellation_is_logged_and_propagated(
    wired: dict, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _repair(handle, *, phase):
        raise asyncio.CancelledError

    monkeypatch.setattr(workers.webhook_repair, "_repair_superseded_retrying_deliveries_safely", _repair)
    sleeps: list = []
    _shim(monkeypatch, sleeps, stop_after=5, clock=_Clock(0.0, 0.0, 0.0, 0.0))
    caplog.set_level(logging.INFO)

    with pytest.raises(asyncio.CancelledError):
        await workers.repair_worker_loop(None)

    assert sleeps == []
    assert any("webhook retry repair worker cancelled" in record.getMessage() for record in caplog.records)


def test_every_loop_is_exported() -> None:
    assert set(workers.__all__) == {
        "repair_worker_loop",
        "delivery_worker_loop",
        "recovery_worker_loop",
        "_recover_due_deliveries",
        "_semaphore_available",
        "_claim_recoverable_deliveries",
        "_recoverable_delivery_ids",
    }


def test_the_recovery_poll_interval_is_the_documented_thirty_seconds() -> None:
    assert webhook_types.RECOVERY_POLL_INTERVAL == 30.0


async def _noop(*args, **kwargs):
    return None
