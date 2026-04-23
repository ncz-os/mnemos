"""GPUGuard — circuit-breaker state transitions.

Pure-logic tests for compression/gpu_guard.py. No HTTP, no real GPU.
State transitions are driven by direct record_success / record_failure
calls against controlled failure thresholds and cooldown windows.
"""

from __future__ import annotations

import asyncio

import pytest

from compression.gpu_guard import (
    CircuitState,
    GPUGuard,
    GuardConfig,
    all_guards,
    get_guard,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


def _make(endpoint: str = "http://test:8000", **cfg) -> GPUGuard:
    base = GuardConfig()
    merged = GuardConfig(
        failure_threshold=cfg.get("failure_threshold", base.failure_threshold),
        cooldown_seconds=cfg.get("cooldown_seconds", base.cooldown_seconds),
        probe_timeout_seconds=cfg.get("probe_timeout_seconds", base.probe_timeout_seconds),
        log_throttle_seconds=cfg.get("log_throttle_seconds", base.log_throttle_seconds),
    )
    return GPUGuard(endpoint, config=merged)


# ---- initial state ---------------------------------------------------------


def test_initial_state_is_closed():
    guard = _make()
    assert guard.state is CircuitState.CLOSED
    assert asyncio.run(guard.is_available()) is True
    assert guard.last_error is None


# ---- CLOSED → OPEN ---------------------------------------------------------


def test_threshold_failures_open_the_circuit():
    guard = _make(failure_threshold=3)

    async def drive():
        for _ in range(3):
            await guard.record_failure(RuntimeError("oops"))

    asyncio.run(drive())
    assert guard.state is CircuitState.OPEN
    assert asyncio.run(guard.is_available()) is False
    assert guard.last_error is not None and "oops" in guard.last_error


def test_below_threshold_failures_stay_closed():
    guard = _make(failure_threshold=3)

    async def drive():
        await guard.record_failure(RuntimeError("one"))
        await guard.record_failure(RuntimeError("two"))

    asyncio.run(drive())
    assert guard.state is CircuitState.CLOSED
    assert asyncio.run(guard.is_available()) is True


def test_success_resets_failure_counter():
    guard = _make(failure_threshold=3)

    async def drive():
        await guard.record_failure(RuntimeError("one"))
        await guard.record_failure(RuntimeError("two"))
        await guard.record_success()
        # Counter reset — now need 3 more failures to open
        await guard.record_failure(RuntimeError("three"))
        await guard.record_failure(RuntimeError("four"))

    asyncio.run(drive())
    assert guard.state is CircuitState.CLOSED


# ---- OPEN → HALF_OPEN → CLOSED / OPEN --------------------------------------


def test_cooldown_elapses_to_half_open():
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)  # immediate cooldown

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        assert guard.state is CircuitState.OPEN
        # After any elapsed time >= 0, is_available() triggers HALF_OPEN
        available = await guard.is_available()
        return available

    assert asyncio.run(drive()) is True
    assert guard.state is CircuitState.HALF_OPEN


def test_half_open_success_closes_circuit():
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        await guard.is_available()  # -> HALF_OPEN
        await guard.record_success()

    asyncio.run(drive())
    assert guard.state is CircuitState.CLOSED


def test_half_open_failure_reopens_circuit():
    # Small but non-zero cooldown: long enough that the re-opened
    # circuit stays OPEN when the post-probe is_available() fires
    # immediately; short enough the test finishes fast.
    guard = _make(failure_threshold=2, cooldown_seconds=0.1)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        # Wait past the cooldown window so the next is_available()
        # transitions OPEN -> HALF_OPEN.
        await asyncio.sleep(0.15)
        assert await guard.is_available() is True
        assert guard.state is CircuitState.HALF_OPEN
        # Probe fails -> circuit re-opens with a FRESH cooldown window.
        await guard.record_failure(RuntimeError("probe failed"))

    asyncio.run(drive())
    assert guard.state is CircuitState.OPEN
    # The fresh cooldown just started; is_available must still be False.
    assert asyncio.run(guard.is_available()) is False
    assert "probe failed" in (guard.last_error or "")


# ---- cooldown NOT elapsed keeps OPEN ---------------------------------------


def test_open_rejects_within_cooldown_window():
    # Large cooldown ensures we stay OPEN for the duration of the test.
    guard = _make(failure_threshold=2, cooldown_seconds=3600.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        return [await guard.is_available() for _ in range(3)]

    results = asyncio.run(drive())
    assert results == [False, False, False]
    assert guard.state is CircuitState.OPEN


# ---- snapshot + reset ------------------------------------------------------


def test_snapshot_shape():
    guard = _make(failure_threshold=2)

    async def drive():
        await guard.record_failure(RuntimeError("first"))

    asyncio.run(drive())

    snap = guard.snapshot()
    assert snap["endpoint"] == "http://test:8000"
    assert snap["state"] == "closed"
    assert snap["consecutive_failures"] == 1
    assert snap["opened_at"] is None
    assert "first" in snap["last_error"]
    assert snap["probe_in_flight"] is False
    assert snap["probe_started_at"] is None


def test_reset_returns_to_closed():
    guard = _make(failure_threshold=2, cooldown_seconds=3600.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))

    asyncio.run(drive())
    assert guard.state is CircuitState.OPEN

    guard.reset()
    assert guard.state is CircuitState.CLOSED
    assert guard.last_error is None
    assert asyncio.run(guard.is_available()) is True


# ---- registry --------------------------------------------------------------


def test_get_guard_dedupes_by_endpoint():
    a1 = get_guard("http://gpu.local:8000")
    a2 = get_guard("http://gpu.local:8000")
    assert a1 is a2


def test_get_guard_normalizes_trailing_slash():
    a1 = get_guard("http://gpu.local:8000")
    a2 = get_guard("http://gpu.local:8000/")
    assert a1 is a2


def test_get_guard_separates_different_endpoints():
    a = get_guard("http://host-a:8000")
    b = get_guard("http://host-b:8000")
    assert a is not b
    assert a.endpoint == "http://host-a:8000"
    assert b.endpoint == "http://host-b:8000"


def test_all_guards_returns_registered():
    get_guard("http://host-a:8000")
    get_guard("http://host-b:8000")
    snapshot = all_guards()
    assert set(snapshot.keys()) == {"http://host-a:8000", "http://host-b:8000"}


# ---- single-probe in HALF_OPEN (v3.1.1) -----------------------------------


def test_half_open_admits_only_one_concurrent_probe():
    """After cooldown elapses, the FIRST is_available() call transitions
    OPEN -> HALF_OPEN and returns True. Subsequent concurrent callers
    must see probe_in_flight and return False — otherwise we'd flood
    a possibly-still-broken endpoint.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        # First caller admitted as probe.
        first = await guard.is_available()
        # All subsequent callers rejected until probe resolves.
        second = await guard.is_available()
        third = await guard.is_available()
        return first, second, third

    first, second, third = asyncio.run(drive())
    assert first is True
    assert second is False
    assert third is False
    assert guard.state is CircuitState.HALF_OPEN
    assert guard.snapshot()["probe_in_flight"] is True


def test_probe_success_clears_in_flight_flag():
    """record_success must clear probe_in_flight so the next
    OPEN -> HALF_OPEN transition can admit a new probe.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        await guard.is_available()  # probe admitted
        assert guard.snapshot()["probe_in_flight"] is True
        await guard.record_success()
        assert guard.snapshot()["probe_in_flight"] is False
        assert guard.state is CircuitState.CLOSED

    asyncio.run(drive())


def test_probe_failure_clears_in_flight_flag_and_reopens():
    """record_failure while HALF_OPEN must both re-open the circuit
    AND clear probe_in_flight, so the NEXT cooldown cycle admits a
    fresh probe.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.05)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        await asyncio.sleep(0.1)  # cooldown elapses
        assert await guard.is_available() is True  # probe admitted
        assert guard.snapshot()["probe_in_flight"] is True
        await guard.record_failure(RuntimeError("probe died"))
        # Circuit re-opened with FRESH cooldown; probe flag cleared.
        snap = guard.snapshot()
        assert snap["state"] == "open"
        assert snap["probe_in_flight"] is False
        # Within new cooldown, no admission.
        assert await guard.is_available() is False

    asyncio.run(drive())


def test_abandoned_probe_admits_new_probe_after_timeout():
    """If a probe caller crashed/was cancelled without recording
    success/failure, the in_flight flag would sit forever and wedge
    HALF_OPEN. After `probe_timeout_seconds` elapses since
    probe_started_at, a fresh call must admit a new probe.
    """
    guard = _make(
        failure_threshold=2,
        cooldown_seconds=0.05,
        probe_timeout_seconds=0.1,
    )

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        await asyncio.sleep(0.1)  # cooldown to reach HALF_OPEN
        # First probe admitted, then caller simulates crash (no record).
        assert await guard.is_available() is True
        assert guard.snapshot()["probe_in_flight"] is True
        # Second caller within the probe window: rejected.
        assert await guard.is_available() is False
        # Wait past probe_timeout_seconds.
        await asyncio.sleep(0.15)
        # Now a new caller gets admitted as the fresh probe.
        assert await guard.is_available() is True

    asyncio.run(drive())


def test_reset_clears_probe_flags():
    """reset() must return the guard to a pristine state including
    probe-tracking fields.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        await guard.is_available()  # in_flight = True

    asyncio.run(drive())
    assert guard.snapshot()["probe_in_flight"] is True

    guard.reset()
    snap = guard.snapshot()
    assert snap["probe_in_flight"] is False
    assert snap["probe_started_at"] is None
    assert snap["state"] == "closed"
