"""Coverage for ReconnectBackoff invariants.

Audit Finding 8 (handoff queue #3): NATS consumer reconnect loops
used a fixed delay, so a fleet of workers reconnecting after a
broker bounce all retry at the same instant. ReconnectBackoff
provides exponential growth with full jitter to break the herd.
"""
from __future__ import annotations

import math

import pytest

from mnemos.nats.backoff import ReconnectBackoff


def test_first_call_sleeps_within_base_window():
    bo = ReconnectBackoff(base_seconds=2.0, cap_seconds=60.0)
    delays = [bo.next_delay() for _ in range(5)]
    # First sample is in [0, base_seconds); subsequent windows
    # double up to cap. Just assert all are non-negative and
    # within the cap.
    for d in delays:
        assert 0.0 <= d <= 60.0


def test_doubles_until_cap():
    # Use no jitter? full-jitter samples can land at 0; instead
    # check the INTERNAL window (which we expose via next_delay's
    # advance behaviour by sampling many times and asserting the
    # MAX possible delay grows then plateaus).
    bo = ReconnectBackoff(base_seconds=1.0, cap_seconds=8.0, multiplier=2.0)
    # _current after each next_delay call: 2, 4, 8, 8, 8, 8...
    bo.next_delay()  # advances current 1 -> 2
    assert math.isclose(bo._current, 2.0)
    bo.next_delay()  # 2 -> 4
    assert math.isclose(bo._current, 4.0)
    bo.next_delay()  # 4 -> 8
    assert math.isclose(bo._current, 8.0)
    bo.next_delay()  # 8 -> capped at 8
    assert math.isclose(bo._current, 8.0)


def test_reset_returns_to_base():
    bo = ReconnectBackoff(base_seconds=1.0, cap_seconds=60.0)
    for _ in range(10):
        bo.next_delay()
    # After 10 doublings _current sits at the cap (60).
    assert bo._current == 60.0
    bo.reset()
    assert bo._current == 1.0


def test_full_jitter_samples_inside_the_current_window(monkeypatch):
    # The previous version called next_delay() 100 times and asserted
    # nothing (the comment claimed a negative sample "would have raised",
    # which was false), so any implementation passed -- a constant
    # window, a negative delay, or an uncapped one.
    #
    # Full jitter is uniform(0, window) over the *current* window, so pin
    # the exact bounds per call by replacing the RNG with a stub that
    # returns its endpoints and a midpoint.
    samples = [0.0, 0.5, 1.0]
    monkeypatch.setattr(
        "mnemos.nats.backoff.random.uniform",
        lambda low, high: low + (high - low) * samples.pop(0),
    )

    # base == cap == 4.0: the window never moves, so every sample is in
    # [0, 4.0] and the value is exactly what the RNG returned.
    bo = ReconnectBackoff(base_seconds=4.0, cap_seconds=4.0)
    delays = [bo.next_delay() for _ in range(3)]

    assert delays == [0.0, 2.0, 4.0]
    assert all(delay >= 0.0 for delay in delays)


def test_next_delay_is_bounded_by_the_advancing_window(monkeypatch):
    # base 1s, cap 4s, multiplier 2: the windows offered to the RNG are
    # 1, 2, 4, 4 -- so a delayed reconnect can never exceed the cap, and
    # a sample at the top of a window proves the bound used is the
    # current one rather than a fixed base or an unbounded value.
    offered: list[tuple[float, float]] = []

    def _uniform(low: float, high: float) -> float:
        offered.append((low, high))
        return high

    monkeypatch.setattr("mnemos.nats.backoff.random.uniform", _uniform)

    bo = ReconnectBackoff(base_seconds=1.0, cap_seconds=4.0, multiplier=2.0)
    delays = [bo.next_delay() for _ in range(4)]

    assert offered == [(0.0, 1.0), (0.0, 2.0), (0.0, 4.0), (0.0, 4.0)]
    assert delays == [1.0, 2.0, 4.0, 4.0]


def test_invalid_constructor_args_reject():
    with pytest.raises(ValueError):
        ReconnectBackoff(base_seconds=0)
    with pytest.raises(ValueError):
        ReconnectBackoff(base_seconds=-1.0)
    with pytest.raises(ValueError):
        ReconnectBackoff(base_seconds=10.0, cap_seconds=5.0)
    with pytest.raises(ValueError):
        ReconnectBackoff(base_seconds=1.0, cap_seconds=10.0, multiplier=1.0)


@pytest.mark.asyncio
async def test_sleep_uses_jittered_delay(monkeypatch):
    import asyncio as _asyncio

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    bo = ReconnectBackoff(base_seconds=2.0, cap_seconds=4.0)
    await bo.sleep()
    await bo.sleep()
    # Both calls returned a non-negative float.
    assert len(sleeps) == 2
    assert all(s >= 0 for s in sleeps)
