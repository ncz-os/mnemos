"""Tests for PANTHEON retry backoff timing."""

from __future__ import annotations

import pytest

from mnemos.domain.pantheon.backoff import (
    INITIAL_RETRY_DELAY,
    JITTER,
    MAX_RETRY_DELAY,
    compute_backoff,
)

_HALF = lambda: 0.5  # noqa: E731 — deterministic rng draw → jitter = 0.375


def test_healthy_siblings_retry_instantly():
    assert compute_backoff(0, has_healthy_siblings=True, rng=_HALF) == 0.0
    # even with a retry_after, a healthy sibling wins (retry it now).
    assert compute_backoff(3, retry_after=30, has_healthy_siblings=True, rng=_HALF) == 0.0


@pytest.mark.parametrize(
    "attempt,base",
    [(0, 0.5), (1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 8.0), (10, 8.0)],
)
def test_exponential_with_cap(attempt, base):
    # base = min(0.5 * 2**attempt, 8.0); jitter = 0.75 * 0.5 = 0.375
    assert compute_backoff(attempt, rng=_HALF) == pytest.approx(base + 0.375)


def test_max_cap_is_8():
    assert compute_backoff(100, rng=lambda: 0.0) == pytest.approx(MAX_RETRY_DELAY)


def test_retry_after_honored_when_sane():
    assert compute_backoff(0, retry_after=10, rng=_HALF) == pytest.approx(10.375)
    assert compute_backoff(2, retry_after=60, rng=lambda: 0.0) == pytest.approx(60.0)


def test_retry_after_ignored_when_out_of_range():
    # >60 ignored -> exponential
    assert compute_backoff(1, retry_after=120, rng=_HALF) == pytest.approx(1.375)
    # 0 / negative ignored -> exponential
    assert compute_backoff(0, retry_after=0, rng=_HALF) == pytest.approx(0.875)
    assert compute_backoff(0, retry_after=-5, rng=_HALF) == pytest.approx(0.875)


def test_jitter_bounds_with_default_rng():
    # default rng -> jitter in [0, JITTER); value in [base, base + JITTER)
    for _ in range(50):
        v = compute_backoff(0)
        assert INITIAL_RETRY_DELAY <= v < INITIAL_RETRY_DELAY + JITTER


def test_constants():
    assert (INITIAL_RETRY_DELAY, MAX_RETRY_DELAY, JITTER) == (0.5, 8.0, 0.75)
