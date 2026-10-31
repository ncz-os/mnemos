"""Microbenchmark for the M6 rate limiters. Run: python3 .bench/bench_m6.py"""
import statistics
import time

from mnemos.core.resilience import InProcessRateLimiter
from mnemos.mcp import oauth as mcp_oauth


def _time(fn, n=200):
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples) * 1e6


def bench_sliding_window(callers: int, tracked: int) -> float:
    limiter = mcp_oauth._SlidingWindowRateLimiter(limit=10**9, window_seconds=60.0, max_callers=tracked)
    for i in range(tracked):
        assert limiter.check(f"ip:10.{i // 256}.{i % 256}.1")[0]
    # one caller keeps hitting; every call re-prunes the whole table on the base
    return _time(lambda: limiter.check("ip:192.0.2.1"), n=50)


def bench_inprocess() -> tuple[float, float, int]:
    """A limiter that has seen fewer than ``rpm`` calls in the window keeps
    every stamp forever on the base (the list is only rebuilt, never
    trimmed), so each call copies a list that only grows."""
    waste = InProcessRateLimiter("bench", rpm=10**9)
    stale = [0.0] * 50_000
    steadies = [InProcessRateLimiter("bench", rpm=10**9) for _ in range(500)]
    for lim in steadies:
        lim.is_allowed()
    small = statistics.median(
        _time(lim.is_allowed, n=1) for lim in steadies
    )
    waste._timestamps = stale
    used = len(waste._timestamps)
    large = _time(waste.is_allowed, n=500)
    return small, large / small, used


if __name__ == "__main__":
    for n in (10, 1000, 4096):
        print(f"oauth._SlidingWindowRateLimiter.check with {n:>4} tracked callers: {bench_sliding_window(1, n):8.1f} us")
    base, ratio, used = bench_inprocess()
    print(f"resilience.InProcessRateLimiter.is_allowed steady list: {base:6.2f} us")
    print(f"resilience.InProcessRateLimiter.is_allowed after {used} timestamps: {base * ratio:8.2f} us ({ratio:.1f}x)")
