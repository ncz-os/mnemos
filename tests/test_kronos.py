from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from mnemos.api.dependencies import UserContext, get_current_user

# FB86 (2026-11-01): the kronos anomaly/forecast fixtures are dated from the
# real clock while the production functions read the clock AGAIN internally
# (anomaly.py:97/164, forecast.py:109/138). Two independent reads across the
# test seam race at the 00:00 UTC day boundary (and the hour floor): the
# "current" fixture bucket lands in the previous day and the spike/drop
# classification inverts. Freeze BOTH reads to one pinned instant — test-only;
# production code stays on the wall clock.
#
# The pin is midnight-ALIGNED, and deliberately the instant the race is worst.
# It matters for a second reason: ``forecast._hourly_counts_from_rows`` floors
# ``now`` to ``now_hour`` and indexes each row by ``(bucket_hour -
# start_hour)`` hours, so a fixture hour LATER than the floored internal hour
# (e.g. a 01:00 UTC pin, as an earlier revision of this fix used) lands at
# index 168 and is dropped by the ``0 <= idx < window`` test -- the history
# falls out of the window and the CI-bounds assertion inverts. Pinning to
# 00:00 UTC keeps the newest fixture row at index 167, and the same instant is
# the worst case for the day boundary.
_PINNED_NOW = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)


def _as_frozen(value: datetime) -> datetime:
    """Re-build a datetime as a ``_FrozenDatetime``, preserving tz-awareness.

    Subclass arithmetic keeps the subclass, but anything that produced a plain
    datetime (``_PINNED_NOW.replace``/``.astimezone`` build a ``_PINNED_NOW``
    which is a plain ``datetime``) must be re-wrapped so a fixture value still
    satisfies the ``isinstance(value, datetime)`` guards in anomaly.py
    (``_coerce_date``/``_as_utc``) and forecast.py (``_as_utc``) -- those
    guards test against the class that was imported at module scope, not
    against the (possibly patched) module-level binding.
    """
    return _FrozenDatetime(
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        tzinfo=value.tzinfo,
    )


def _pinned_now() -> datetime:
    """The pinned instant, re-wrapped as a ``_FrozenDatetime``.

    Every fixture value in this module is built from this one instant, so the
    fixture side and the (patched) internal ``datetime.now`` side of the test
    seam are the same value by construction -- that is the whole point of the
    FB86 freeze.
    """
    return _as_frozen(_PINNED_NOW)


class _FrozenDatetime(datetime):
    """Drop-in ``datetime`` whose ``now()`` returns ``_PINNED_NOW``.

    Subclasses the real ``datetime`` so the ``isinstance(value, datetime)``
    guards inside anomaly.py/_coerce_date/_as_utc and forecast.py/_as_utc
    keep recognizing our fixture rows (CPython keeps aware arithmetic on the
    subclass, so ``_as_frozen(_PINNED_NOW) - timedelta(...)`` is still a
    ``_FrozenDatetime``).
    """

    @classmethod
    def now(cls, tz=None):
        # Both modules under test always pass ``timezone.utc``; the naive
        # branch exists only so a future tz-naive read still gets the pin
        # rather than the host clock, and mirrors ``datetime.now(None)``
        # (local naive time) by keeping the pin's own wall-clock fields.
        if tz is None:
            return _as_frozen(_PINNED_NOW.replace(tzinfo=None))
        return _as_frozen(_PINNED_NOW.astimezone(tz))


@pytest.fixture(autouse=True)
def _kronos_frozen_clock(monkeypatch: pytest.MonkeyPatch):
    """Pin the internal clock of the kronos anomaly/forecast modules.

    Mirrors the repo-wide ``monkeypatch.setattr("mnemos.persistence.*.datetime",
    _FixedDatetime)`` convention (see tests/test_db2_dialect_parity.py:159).
    """
    monkeypatch.setattr("mnemos.domain.kronos.anomaly.datetime", _FrozenDatetime)
    monkeypatch.setattr("mnemos.domain.kronos.forecast.datetime", _FrozenDatetime)


@pytest.fixture(autouse=True)
def _kronos_cpu_backend(monkeypatch: pytest.MonkeyPatch):
    """Keep this module's forecast tests on the deterministic NumPy backend.

    The selector's default (``auto``) probes ``import cupy`` and, when the
    wheel is installed but unusable, the GPU path raises
    ``RuntimeError: CuPy failed to load libnvrtc.so.12`` out of ``ewma`` -- an
    environment defect unrelated to the code under test
    (``tests/test_kronos_backends.py`` covers the GPU path on purpose, with
    its own fallback assertions). Pin the CPU backend here instead of letting
    host CUDA state decide whether the forecast assertions can run at all.
    """
    from mnemos.domain.kronos.backends import selector

    monkeypatch.setenv("MNEMOS_KRONOS_BACKEND", "cpu")
    assert selector.get_backend().BACKEND_NAME == "cpu"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(
        self,
        *,
        daily_rows=None,
        hourly_rows=None,
        event_rows=None,
        memory_rows=None,
        eligibility_rows=None,
    ):
        self.conn = _Conn(
            daily_rows=daily_rows or [],
            hourly_rows=hourly_rows or [],
            event_rows=event_rows or [],
            memory_rows=memory_rows or [],
            eligibility_rows=eligibility_rows or [],
        )

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, *, daily_rows, hourly_rows, event_rows, memory_rows, eligibility_rows):
        self.daily_rows = daily_rows
        self.hourly_rows = hourly_rows
        self.event_rows = event_rows
        self.memory_rows = memory_rows
        self.eligibility_rows = eligibility_rows

    async def fetch(self, query: str, *args):
        if "date_trunc('day'" in query:
            return self.daily_rows
        if "date_trunc('hour'" in query:
            return self.hourly_rows
        if "FROM memory_recall_log" in query and "recalled_at" in query:
            return self.event_rows
        if "archived_at IS NULL" in query:
            return self.eligibility_rows
        if "SELECT id, recall_count, last_recalled_at" in query:
            return self.memory_rows
        return []


def _root() -> UserContext:
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _day_start() -> datetime:
    """Midnight UTC of the PINNED instant, so fixture rows are race-free.

    Returns the frozen subclass on purpose: the row is fed to
    ``anomaly._coerce_date``, whose first branch is
    ``isinstance(value, datetime)`` -> ``_as_utc(value).date()``. A plain
    ``date`` there would take the second branch instead, which is a different
    code path from the one a real driver row exercises.
    """
    pinned = _pinned_now()
    return _as_frozen(pinned.replace(hour=0, minute=0, second=0, microsecond=0))


def _isolate_bind_guard_env(monkeypatch) -> None:
    """Give the TestClient-based tests a known bind-guard precondition.

    ``TestClient(app)`` drives the real lifespan, which refuses to start an
    unauthenticated API unless ``mnemos serve`` recorded a loopback bind
    (``mnemos/core/network_guard.py``). These tests monkeypatch the user
    dependency instead of enabling auth, and they do not go through
    ``mnemos serve``. Two ambient leaks break them depending on test order:

    * the guard's escape hatch left set by another test, and
    * a stale validated-bind value set via ``record_validated_bind``, which
      writes through ``set_runtime_env_value`` and therefore survives
      monkeypatch's env restore.

    Pin both to "nothing recorded, escape hatch off" and record loopback.
    """
    from mnemos.core import network_guard

    monkeypatch.delenv(network_guard.UNSAFE_NETWORK_BIND_ENV, raising=False)
    monkeypatch.delenv(network_guard.BIND_CHECKED_ENV, raising=False)
    network_guard.record_validated_bind("127.0.0.1")


def _stable_daily_rows(memory_id: str, baseline_count: int, current_count: int) -> list[dict]:
    today = _day_start()
    rows = [
        {
            "memory_id": memory_id,
            "bucket_day": today - timedelta(days=offset),
            "recall_count": baseline_count,
        }
        for offset in range(7, 0, -1)
    ]
    rows.append({"memory_id": memory_id, "bucket_day": today, "recall_count": current_count})
    return rows


def test_z_score_and_ewma_helpers():
    from mnemos.domain.kronos.scoring import ewma, z_score

    assert z_score(12.0, 10.0, 2.0) == 1.0
    assert z_score(10.0, 10.0, 0.0) == 0.0
    np.testing.assert_allclose(
        ewma(np.asarray([10.0, 20.0, 30.0]), alpha=0.5),
        np.asarray([10.0, 15.0, 22.5]),
    )


def test_hourly_buckets_correctness_with_fixed_timestamps():
    from mnemos.domain.kronos.scoring import hourly_buckets

    base = datetime(2026, 1, 1, 12, 15, tzinfo=timezone.utc)
    buckets = hourly_buckets(
        [
            base,
            base + timedelta(hours=1),
            base + timedelta(hours=1, minutes=40),
            base + timedelta(hours=3),
        ],
        window_hours=4,
    )

    np.testing.assert_array_equal(buckets, np.asarray([1.0, 2.0, 0.0, 1.0]))


@pytest.mark.asyncio
async def test_detect_recall_anomalies_spike_case():
    from mnemos.domain.kronos import detect_recall_anomalies

    pool = _Pool(daily_rows=_stable_daily_rows("mem_spike", 100, 500))
    anomalies = await detect_recall_anomalies(pool, "default", sensitivity=2.5)

    assert len(anomalies) == 1
    assert anomalies[0].memory_id == "mem_spike"
    assert anomalies[0].anomaly_type == "spike"
    assert anomalies[0].z_score > 2.5
    assert anomalies[0].suggested_action == "trending"


@pytest.mark.asyncio
async def test_detect_recall_anomalies_drop_case():
    from mnemos.domain.kronos import detect_recall_anomalies

    pool = _Pool(daily_rows=_stable_daily_rows("mem_drop", 100, 5))
    anomalies = await detect_recall_anomalies(pool, "default", sensitivity=2.5)

    assert len(anomalies) == 1
    assert anomalies[0].memory_id == "mem_drop"
    assert anomalies[0].anomaly_type == "drop"
    assert anomalies[0].z_score < -2.5
    assert anomalies[0].suggested_action == "eligible_for_persephone"


@pytest.mark.asyncio
async def test_spike_classification_is_midnight_race_free():
    """FB86 regression guard: the fixture clock and the internal
    ``datetime.now`` read must be the same pinned instant. Previously the two
    wall-clock reads raced across the 00:00 UTC day boundary: the "current"
    fixture bucket landed in the previous day and the spike case inverted to
    a drop.
    """
    from mnemos.domain.kronos import anomaly as anomaly_mod
    from mnemos.domain.kronos import detect_recall_anomalies

    # The module under test must see the frozen clock, same as the fixture
    # builder does (this is what keeps both sides of the seam consistent).
    assert anomaly_mod.datetime.now(timezone.utc) == _PINNED_NOW

    pool = _Pool(daily_rows=_stable_daily_rows("mem_guard", 100, 500))
    anomalies = await detect_recall_anomalies(pool, "default", sensitivity=2.5)

    assert [a.memory_id for a in anomalies] == ["mem_guard"]
    assert anomalies[0].anomaly_type == "spike"
    assert anomalies[0].suggested_action == "trending"


@pytest.mark.asyncio
async def test_forecast_recall_load_returns_sane_ci_bounds():
    from mnemos.domain.kronos import forecast_recall_load

    now_hour = _pinned_now().replace(minute=0, second=0, microsecond=0)
    rows = [
        {
            "bucket_hour": now_hour - timedelta(hours=idx),
            "recall_count": 10 + (idx % 4),
        }
        for idx in range(24 * 7)
    ]
    pool = _Pool(hourly_rows=rows)
    result = await forecast_recall_load(pool, "default", hours_ahead=24)

    assert result.forecast_window_hours == 24
    assert result.ci_lower < result.predicted_total_recalls < result.ci_upper
    assert result.predicted_p95_per_hour > 0


@pytest.mark.asyncio
async def test_forecast_recall_load_window_is_aligned_to_the_pinned_hour():
    """FB86 regression guard: the fixture hour must line up with the hour the
    ``forecast`` module floors its own clock to.

    ``_hourly_counts_from_rows`` indexes rows by ``(row_hour - start_hour)``
    with ``start_hour = now_hour - (window - 1) hours``, so a row at
    ``now_hour + 1h`` indexes at 168 -- past the end of the window -- and is
    dropped by the ``0 <= idx < window`` test. When the fixture is built from
    a *later* instant than the frozen internal clock, the whole history falls
    out of the window, the forecast collapses to its zero result, and the
    CI-bounds assertions above invert. Pin both sides to one instant and
    prove a one-hour shift is what breaks it.
    """
    from mnemos.domain.kronos import forecast as forecast_mod
    from mnemos.domain.kronos import forecast_recall_load

    internal_hour = forecast_mod.datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    assert internal_hour == _pinned_now().replace(minute=0, second=0, microsecond=0)

    def rows_at(anchor: datetime) -> list[dict]:
        return [{"bucket_hour": anchor - timedelta(hours=idx), "recall_count": 10 + (idx % 4)} for idx in range(24 * 7)]

    aligned = await forecast_recall_load(_Pool(hourly_rows=rows_at(internal_hour)), "default")
    shifted = await forecast_recall_load(_Pool(hourly_rows=rows_at(internal_hour + timedelta(hours=1))), "default")
    # The extra row the shift contributes is the one real NEWEST hour, whose
    # count is the fixture's highest (10 + 0 % 4 against a tail of 10 + 3): a
    # one-hour misalignment changes the answer, it does not merely move it.
    assert shifted.predicted_total_recalls > aligned.predicted_total_recalls > 0


@pytest.mark.asyncio
async def test_forecast_persephone_eligibility_counts_future_window():
    from mnemos.domain.kronos import forecast_persephone_eligibility

    now = _pinned_now()
    pool = _Pool(
        eligibility_rows=[
            {"id": "soon", "last_recalled_at": now - timedelta(days=170)},
            {"id": "too_late", "last_recalled_at": now - timedelta(days=149)},
            {"id": "already", "last_recalled_at": now - timedelta(days=181)},
            {"id": "never", "last_recalled_at": None},
        ]
    )

    assert await forecast_persephone_eligibility(pool, "default", archive_after_days=180, days_ahead=30) == 1


def test_kronos_routes_disabled_return_503(monkeypatch, tmp_path):
    from mnemos.api.main import app
    from mnemos.core.config import _reset_settings_for_tests

    _isolate_bind_guard_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_KRONOS_ENABLED", "false")
    monkeypatch.setenv("MNEMOS_SQLITE_PATH", str(tmp_path / "mnemos.sqlite3"))
    _reset_settings_for_tests()
    app.dependency_overrides[get_current_user] = lambda: _root()
    try:
        with TestClient(app) as client:
            response = client.get("/admin/kronos/anomalies", params={"namespace": "default"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        monkeypatch.delenv("MNEMOS_KRONOS_ENABLED", raising=False)
        monkeypatch.delenv("MNEMOS_SQLITE_PATH", raising=False)
        _reset_settings_for_tests()

    assert response.status_code == 503
    assert response.json()["detail"] == "KRONOS disabled in this profile"


def test_kronos_routes_enabled_return_expected_json_shape(monkeypatch, tmp_path):
    from mnemos.api.main import app
    from mnemos.core.config import _reset_settings_for_tests
    import mnemos.core.lifecycle as lc

    _isolate_bind_guard_env(monkeypatch)
    monkeypatch.setenv("MNEMOS_KRONOS_ENABLED", "true")
    monkeypatch.setenv("MNEMOS_SQLITE_PATH", str(tmp_path / "mnemos.sqlite3"))
    _reset_settings_for_tests()
    app.dependency_overrides[get_current_user] = lambda: _root()
    try:
        with TestClient(app) as client:
            monkeypatch.setattr(
                lc,
                "_pool",
                _Pool(daily_rows=_stable_daily_rows("mem_spike", 100, 500)),
            )
            response = client.get("/admin/kronos/anomalies", params={"namespace": "default"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        monkeypatch.delenv("MNEMOS_KRONOS_ENABLED", raising=False)
        monkeypatch.delenv("MNEMOS_SQLITE_PATH", raising=False)
        _reset_settings_for_tests()

    assert response.status_code == 200
    data = response.json()
    assert data["namespace"] == "default"
    assert data["count"] == 1
    assert data["anomalies"][0]["anomaly_type"] == "spike"
    assert data["anomalies"][0]["observed_count"] == 500


@pytest.mark.asyncio
async def test_mcp_kronos_anomalies_cross_namespace_non_root_is_empty(monkeypatch):
    from mnemos.core.config import _reset_settings_for_tests
    from mnemos.mcp.tools import execute_tool

    monkeypatch.setenv("MNEMOS_KRONOS_ENABLED", "true")
    _reset_settings_for_tests()
    try:
        result = await execute_tool(
            "kronos_anomalies",
            {"namespace": "other-ns"},
            user=_alice(),
        )
    finally:
        monkeypatch.delenv("MNEMOS_KRONOS_ENABLED", raising=False)
        _reset_settings_for_tests()

    assert result == {"success": True, "namespace": "other-ns", "count": 0, "anomalies": []}
