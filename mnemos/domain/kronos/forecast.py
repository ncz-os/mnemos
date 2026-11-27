"""Recall-load forecasting for KRONOS v0.1.

The partial-hour extrapolation bound in ``forecast_namespace`` relies on
``datetime`` / ``timezone`` (imported via ``from datetime import ...`` in
this module). ``tests/test_kronos.py`` exercises that path; an import-time
failure there surfaces as a ``NameError`` at call time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from mnemos.domain.kronos.backends.selector import get_backend


@dataclass(frozen=True)
class ForecastResult:
    namespace: str
    forecast_window_hours: int
    predicted_total_recalls: float
    predicted_p95_per_hour: float
    ci_lower: float
    ci_upper: float


_HOURLY_RECALL_LOG_SQL = """
SELECT date_trunc('hour', recalled_at AT TIME ZONE 'UTC') AS bucket_hour,
       COUNT(*)::int AS recall_count
  FROM memory_recall_log
 WHERE namespace = $1
   AND recalled_at >= NOW() - INTERVAL '7 days'
 GROUP BY bucket_hour
 ORDER BY bucket_hour
"""

_HOURLY_MEMORIES_FALLBACK_SQL = """
SELECT date_trunc('hour', last_recalled_at AT TIME ZONE 'UTC') AS bucket_hour,
       SUM(recall_count)::int AS recall_count
  FROM memories
 WHERE namespace = $1
   AND last_recalled_at IS NOT NULL
   AND last_recalled_at >= NOW() - INTERVAL '7 days'
   AND deleted_at IS NULL
 GROUP BY bucket_hour
 ORDER BY bucket_hour
"""

_PERSEPHONE_ELIGIBILITY_SQL = """
SELECT id, last_recalled_at
  FROM memories
 WHERE namespace = $1
   AND deleted_at IS NULL
   AND archived_at IS NULL
   AND consolidated_into IS NULL
   AND last_recalled_at IS NOT NULL
"""


_EWMA_ALPHA = 0.3

# How far the in-progress hour may move the forecast rate, as a multiple of
# the level fitted on completed hours. Guards against extrapolating a burst in
# the first minutes of an hour into an arbitrarily large rate (NG2-MN-015).
_MAX_PARTIAL_HOUR_MULTIPLE = 3.0

# Minimum fraction of the current hour treated as elapsed when extrapolating
# the partial bucket, so the first seconds of an hour cannot divide by ~0.
_MIN_ELAPSED_HOUR_FRACTION = 5.0 / 60.0


async def forecast_recall_load(
    pool: Any,
    namespace: str,
    hours_ahead: int = 24,
) -> ForecastResult:
    """Forecast aggregate recall load with EWMA over hourly recall buckets."""
    if hours_ahead <= 0:
        raise ValueError("hours_ahead must be positive")

    rows = await _fetch_hourly_recall_counts(pool, namespace)
    history = _hourly_counts_from_rows(rows, window_hours=24 * 7)
    # NG2-MN-015: ``history[-1]`` is the hour in progress, not a finished
    # hour, so it holds at most a fraction of an hour's recalls (8 at minute
    # 5 of a 100/hour hour). Smoothing over it, and differencing it against
    # the smoothed series, biased the forecast rate down by ~30%. Fit the
    # EWMA and the residual spread on the completed hours only, and treat the
    # in-progress bucket separately as load already observed.
    elapsed = _elapsed_hour_fraction()
    # ``history`` is a fixed-width window whose newest slot is the hour in
    # progress, so the completed hours are simply everything before it. The
    # size guard keeps the arithmetic total even if that width ever changes.
    completed = history[:-1]
    if completed.size == 0 or float(np.sum(completed)) == 0.0:
        return ForecastResult(
            namespace=namespace,
            forecast_window_hours=hours_ahead,
            predicted_total_recalls=0.0,
            predicted_p95_per_hour=0.0,
            ci_lower=0.0,
            ci_upper=0.0,
        )

    backend = get_backend()
    # ``completed`` ends at the last whole hour, so the EWMA's newest point is
    # the level to forecast the next hours from.
    smoothed = backend.ewma(completed, alpha=_EWMA_ALPHA)
    level = float(smoothed[-1])
    per_hour = max(0.0, level)
    partial = float(history[-1])
    if partial > 0.0:
        # One more EWMA step for the hour in progress: its partial count is
        # scaled to a full hour by the fraction elapsed, so an hour that is
        # already heavy shows up in the rate. A bucket with no recalls yet
        # carries no information about the hour's eventual total, and is
        # skipped rather than read as a zero-load hour.
        projected_current_hour = partial / elapsed
        # Cap how far one partial hour may move the rate: a burst in the first
        # minutes of an hour would otherwise extrapolate without limit. The
        # cap is relative to the level fitted on completed hours, and only
        # ever raises the estimate, so a quiet partial hour cannot erase it.
        # The floor of 1 recall/hour keeps the cap meaningful when the EWMA
        # has decayed to ~0 behind a long quiet stretch -- that is exactly
        # when a live partial hour is the most useful signal.
        cap = _MAX_PARTIAL_HOUR_MULTIPLE * max(level, float(np.mean(completed)), 1.0)
        projected_current_hour = min(projected_current_hour, cap)
        per_hour = max(0.0, _EWMA_ALPHA * projected_current_hour + (1.0 - _EWMA_ALPHA) * level)
    residuals = completed - smoothed
    # The spread is fitted on completed hours only, matching the EWMA. The
    # in-progress hour is excluded from it deliberately: its partial count is
    # not a comparable sample, and folding it in is what inflated the interval
    # before NG2-MN-015. The reported interval therefore describes the
    # variance of a whole hour of load, not the first minutes of the current
    # one -- it is a forecast interval for the hours ahead, not for the hour
    # in progress.
    residual_std = float(np.std(residuals))
    if residual_std <= 1e-9:
        residual_std = float(np.sqrt(max(per_hour, 1.0)))

    predicted_total = per_hour * float(hours_ahead)
    ci_half_width = 1.96 * residual_std * float(np.sqrt(hours_ahead))
    return ForecastResult(
        namespace=namespace,
        forecast_window_hours=hours_ahead,
        predicted_total_recalls=predicted_total,
        predicted_p95_per_hour=max(0.0, per_hour + 1.645 * residual_std),
        ci_lower=max(0.0, predicted_total - ci_half_width),
        ci_upper=predicted_total + ci_half_width,
    )


async def forecast_persephone_eligibility(
    pool: Any,
    namespace: str,
    archive_after_days: int = 180,
    days_ahead: int = 30,
) -> int:
    """Count memories projected to become PERSEPHONE-eligible soon."""
    if archive_after_days <= 0:
        raise ValueError("archive_after_days must be positive")
    if days_ahead <= 0:
        raise ValueError("days_ahead must be positive")

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=days_ahead)
    rows = await _fetch_persephone_rows(pool, namespace)
    count = 0
    for row in rows:
        last_recalled_at = _as_utc(_row_get(row, "last_recalled_at"))
        if last_recalled_at is None:
            continue
        eligible_at = last_recalled_at + timedelta(days=archive_after_days)
        if now < eligible_at <= window_end:
            count += 1
    return count


async def _fetch_hourly_recall_counts(pool: Any, namespace: str) -> list[Any]:
    async with pool.acquire() as conn:
        try:
            return list(await conn.fetch(_HOURLY_RECALL_LOG_SQL, namespace))
        except Exception:
            return list(await conn.fetch(_HOURLY_MEMORIES_FALLBACK_SQL, namespace))


async def _fetch_persephone_rows(pool: Any, namespace: str) -> list[Any]:
    async with pool.acquire() as conn:
        return list(await conn.fetch(_PERSEPHONE_ELIGIBILITY_SQL, namespace))


def _hourly_counts_from_rows(rows: list[Any], window_hours: int) -> np.ndarray:
    buckets = np.zeros(window_hours, dtype=float)
    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_hour = now_hour - timedelta(hours=window_hours - 1)
    for row in rows:
        bucket_hour = _as_utc(_row_get(row, "bucket_hour"))
        if bucket_hour is None:
            continue
        bucket_hour = bucket_hour.replace(minute=0, second=0, microsecond=0)
        idx = int((bucket_hour - start_hour).total_seconds() // 3600)
        if 0 <= idx < window_hours:
            buckets[idx] += float(_row_get(row, "recall_count", 0) or 0)
    return buckets


def _elapsed_hour_fraction() -> float:
    """Fraction of the current UTC hour that has elapsed, in ``(0, 1]``.

    Floored at 5 minutes: extrapolating a handful of seconds of traffic to a
    whole hour would let one request project a massive load, so the first
    minutes of an hour are not used to revise the rate.
    """
    now = datetime.now(timezone.utc)
    elapsed = (now.minute * 60 + now.second + now.microsecond / 1_000_000) / 3600.0
    return min(1.0, max(elapsed, _MIN_ELAPSED_HOUR_FRACTION))


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
