"""Characterisation tests (FB1119) for ``mnemos/api/routes/kronos.py``.

Pins the three admin routes as implemented in ``mnemos/api/routes/kronos.py``:

- the router is mounted at ``/admin/kronos`` with three GETs, each gated by
  ``require_root``;
- ``_require_enabled`` answers in a fixed order — the extra probe first
  (``503`` with the ``{error, install}`` dict from
  ``mnemos.core.extras.missing_extra_detail``), then the profile switch
  (``503`` with a **plain string** ``"KRONOS disabled in this profile"``);
- the two failure shapes differ on purpose and are pinned separately, because
  a caller parsing ``detail`` sees a dict in one case and a string in the other;
- each handler passes the profile defaults through to the domain function
  (``default_lookback_hours`` / ``default_sensitivity`` / ``default_baseline_days``),
  asks ``require_postgres_pool_or_503`` for the pool with a route-specific
  label, and returns ``asdict(...)`` of the domain result;
- ``hours_ahead`` defaults to 24 and is bounded to 1..720 by the query schema.

No database, no network, no domain maths: the pool, the extra probe, the
settings and the three domain functions are all fakes.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from dataclasses import asdict, dataclass
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.params import Query as QueryParam
from fastapi.testclient import TestClient

from mnemos.api.dependencies import require_root
from mnemos.api.routes import kronos


@dataclass
class _Anomaly:
    namespace: str
    kind: str
    score: float


@dataclass
class _Drift:
    namespace: str
    baseline_days: int


@dataclass
class _Forecast:
    namespace: str
    hours_ahead: int
    expected: list[int]


def _settings(*, enabled: bool = True, lookback: int = 168, sensitivity: float = 2.5, baseline: int = 30):
    return SimpleNamespace(
        kronos=SimpleNamespace(
            enabled=enabled,
            default_lookback_hours=lookback,
            default_sensitivity=sensitivity,
            default_baseline_days=baseline,
        )
    )


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch):
    """KRONOS installed and switched on; the pool is a sentinel object."""
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: True)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings())
    pool = object()
    monkeypatch.setattr(kronos, "require_postgres_pool_or_503", lambda **kwargs: pool)
    return pool


def _patch_domain(monkeypatch: pytest.MonkeyPatch, **functions) -> ModuleType:
    """Replace ``mnemos.domain.kronos`` in ``sys.modules`` with a fake.

    The handlers import the domain function lazily, at call time, so a
    ``sys.modules`` entry is what they see. That keeps these tests free of
    the KRONOS domain's numpy dependency and of whatever else another test
    left in that module.
    """
    module = ModuleType("mnemos.domain.kronos")
    for name, fn in functions.items():
        setattr(module, name, fn)
    monkeypatch.setitem(sys.modules, "mnemos.domain.kronos", module)
    return module


def _run(coro):
    return asyncio.run(coro)


# ── router wiring ────────────────────────────────────────────────────────


def test_router_is_mounted_under_the_admin_kronos_prefix() -> None:
    assert kronos.router.prefix == "/admin/kronos"
    assert list(kronos.router.tags) == ["admin", "kronos"]


def test_router_exposes_exactly_three_get_routes() -> None:
    routes = {route.path: set(route.methods) for route in kronos.router.routes}
    assert routes == {
        "/admin/kronos/anomalies": {"GET"},
        "/admin/kronos/drift": {"GET"},
        "/admin/kronos/forecast": {"GET"},
    }


def test_every_route_is_gated_by_require_root() -> None:
    seen = {}
    for route in kronos.router.routes:
        calls = {dep.call for dep in route.dependant.dependencies}
        seen[route.path] = require_root in calls
    assert set(seen) == {
        "/admin/kronos/anomalies",
        "/admin/kronos/drift",
        "/admin/kronos/forecast",
    }
    assert all(seen.values()), f"routes missing require_root: {seen}"


# ── _require_enabled: the two 503 shapes ─────────────────────────────────


def test_extra_missing_is_a_503_with_the_install_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: False)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings(enabled=True))

    with pytest.raises(HTTPException) as exc:
        kronos._require_enabled()

    assert exc.value.status_code == 503
    assert exc.value.detail == {
        "error": "KRONOS not installed",
        "install": "pip install mnemos-core[kronos]  (or [server]/[ml]/[full] bundle)",
    }


def test_the_extra_probe_runs_before_the_profile_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Missing extra AND disabled: the extra message wins, which is what tells
    # an operator on an edge profile to install rather than to flip a flag.
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: False)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings(enabled=False))

    with pytest.raises(HTTPException) as exc:
        kronos._require_enabled()

    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["error"] == "KRONOS not installed"


def test_disabled_profile_is_a_503_with_a_plain_string_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: True)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings(enabled=False))

    with pytest.raises(HTTPException) as exc:
        kronos._require_enabled()

    assert exc.value.status_code == 503
    assert exc.value.detail == "KRONOS disabled in this profile"
    assert isinstance(exc.value.detail, str)


def test_enabled_and_installed_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: True)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings(enabled=True))

    assert kronos._require_enabled() is None


@pytest.mark.parametrize("flag", [True, 1, "yes"])
def test_the_profile_switch_is_a_truthiness_test(monkeypatch: pytest.MonkeyPatch, flag) -> None:
    # Not ``is True``: any truthy value enables the routes.
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: True)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings(enabled=flag))

    assert kronos._require_enabled() is None


def test_the_gate_runs_before_the_pool_is_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: True)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings(enabled=False))
    asked = []
    monkeypatch.setattr(kronos, "require_postgres_pool_or_503", lambda **kwargs: asked.append(kwargs))

    with pytest.raises(HTTPException):
        _run(kronos.recall_anomalies(namespace="default", _=None))

    assert asked == []


# ── recall_anomalies ─────────────────────────────────────────────────────


def test_anomalies_passes_the_profile_defaults_and_the_pool(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    calls = {}

    async def _detect(pool, namespace, *, lookback_hours, sensitivity):
        calls.update(pool=pool, namespace=namespace, lookback_hours=lookback_hours, sensitivity=sensitivity)
        return []

    _patch_domain(monkeypatch, detect_recall_anomalies=_detect)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings(lookback=24, sensitivity=1.5))

    _run(kronos.recall_anomalies(namespace="team-a", _=None))

    assert calls == {"pool": enabled, "namespace": "team-a", "lookback_hours": 24, "sensitivity": 1.5}


def test_anomalies_returns_namespace_count_and_asdicts(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    async def _detect(pool, namespace, *, lookback_hours, sensitivity):
        return [_Anomaly(namespace=namespace, kind="spike", score=3.5), _Anomaly(namespace, "drop", 2.0)]

    _patch_domain(monkeypatch, detect_recall_anomalies=_detect)

    result = _run(kronos.recall_anomalies(namespace="team-a", _=None))

    assert result["namespace"] == "team-a"
    assert result["count"] == 2
    assert result["anomalies"] == [
        {"namespace": "team-a", "kind": "spike", "score": 3.5},
        {"namespace": "team-a", "kind": "drop", "score": 2.0},
    ]


def test_anomalies_count_is_the_list_length_when_empty(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    async def _detect(pool, namespace, *, lookback_hours, sensitivity):
        return []

    _patch_domain(monkeypatch, detect_recall_anomalies=_detect)

    result = _run(kronos.recall_anomalies(namespace="team-a", _=None))

    assert result["count"] == 0
    assert result["anomalies"] == []


def test_anomalies_names_its_route_in_the_pool_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: True)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings())
    # The handler imports the domain function lazily; replace it with a stub
    # so the import resolves without the (absent) KRONOS domain package. The
    # pool fails before the domain is ever called.
    _patch_domain(monkeypatch, detect_recall_anomalies=None)
    labels = []

    def _no_pool(*, route_label):
        labels.append(route_label)
        raise HTTPException(status_code=503, detail="Database pool not available")

    monkeypatch.setattr(kronos, "require_postgres_pool_or_503", _no_pool)

    with pytest.raises(HTTPException) as exc:
        _run(kronos.recall_anomalies(namespace="team-a", _=None))

    assert exc.value.status_code == 503
    assert labels == ["GET /admin/kronos/anomalies"]


# ── namespace_drift ──────────────────────────────────────────────────────


def test_drift_passes_the_baseline_days_and_returns_asdict(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    calls = {}

    async def _drift(pool, namespace, *, baseline_days):
        calls.update(pool=pool, namespace=namespace, baseline_days=baseline_days)
        return _Drift(namespace=namespace, baseline_days=baseline_days)

    _patch_domain(monkeypatch, detect_namespace_drift=_drift)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings(baseline=45))

    result = _run(kronos.namespace_drift(namespace="team-b", _=None))

    assert calls == {"pool": enabled, "namespace": "team-b", "baseline_days": 45}
    assert result == asdict(_Drift(namespace="team-b", baseline_days=45))


def test_drift_returns_the_top_level_asdict_not_a_wrapper(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    async def _drift(pool, namespace, *, baseline_days):
        return _Drift(namespace=namespace, baseline_days=baseline_days)

    _patch_domain(monkeypatch, detect_namespace_drift=_drift)

    result = _run(kronos.namespace_drift(namespace="team-b", _=None))

    assert set(result) == {"namespace", "baseline_days"}
    assert "drift" not in result


def test_drift_names_its_route_in_the_pool_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: True)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings())
    _patch_domain(monkeypatch, detect_namespace_drift=None)
    labels = []

    def _no_pool(*, route_label):
        labels.append(route_label)
        raise HTTPException(status_code=503, detail="Database pool not available")

    monkeypatch.setattr(kronos, "require_postgres_pool_or_503", _no_pool)

    with pytest.raises(HTTPException):
        _run(kronos.namespace_drift(namespace="team-b", _=None))

    assert labels == ["GET /admin/kronos/drift"]


# ── recall_forecast ──────────────────────────────────────────────────────


def test_the_forecast_default_is_declared_on_the_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Called directly, the handler receives the ``Query`` object itself — the 24
    # belongs to FastAPI, not to the coroutine. Pinned so a direct-call test
    # never pretends otherwise; the end-to-end default is pinned just below.
    default = inspect.signature(kronos.recall_forecast).parameters["hours_ahead"].default

    assert isinstance(default, QueryParam)
    assert default.default == 24


def test_forecast_applies_twenty_four_hours_through_the_route(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    seen = {}

    async def _forecast(pool, namespace, *, hours_ahead):
        seen.update(pool=pool, namespace=namespace, hours_ahead=hours_ahead)
        return _Forecast(namespace=namespace, hours_ahead=hours_ahead, expected=[])

    _patch_domain(monkeypatch, forecast_recall_load=_forecast)

    response = _client().get("/admin/kronos/forecast?namespace=team-c")

    assert response.status_code == 200
    assert seen == {"pool": enabled, "namespace": "team-c", "hours_ahead": 24}
    assert response.json()["hours_ahead"] == 24


def test_forecast_passes_an_explicit_horizon_through(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    async def _forecast(pool, namespace, *, hours_ahead):
        return _Forecast(namespace=namespace, hours_ahead=hours_ahead, expected=[1, 2])

    _patch_domain(monkeypatch, forecast_recall_load=_forecast)

    result = _run(kronos.recall_forecast(namespace="team-c", hours_ahead=720, _=None))

    assert result == {"namespace": "team-c", "hours_ahead": 720, "expected": [1, 2]}


def test_forecast_names_its_route_in_the_pool_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: True)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings())
    _patch_domain(monkeypatch, forecast_recall_load=None)
    labels = []

    def _no_pool(*, route_label):
        labels.append(route_label)
        raise HTTPException(status_code=503, detail="Database pool not available")

    monkeypatch.setattr(kronos, "require_postgres_pool_or_503", _no_pool)

    with pytest.raises(HTTPException):
        _run(kronos.recall_forecast(namespace="team-c", _=None))

    assert labels == ["GET /admin/kronos/forecast"]


# ── query schema (through a real app, so FastAPI's own 422s are pinned) ──


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(kronos.router)
    app.dependency_overrides[require_root] = lambda: object()
    return TestClient(app)


def test_a_missing_namespace_is_a_422(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    _patch_domain(monkeypatch, detect_recall_anomalies=None)

    assert _client().get("/admin/kronos/anomalies").status_code == 422


def test_an_empty_namespace_is_a_422(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    _patch_domain(monkeypatch, detect_recall_anomalies=None)

    assert _client().get("/admin/kronos/anomalies?namespace=").status_code == 422


@pytest.mark.parametrize("hours", ["0", "721", "-1"])
def test_an_out_of_range_horizon_is_a_422(monkeypatch: pytest.MonkeyPatch, enabled, hours) -> None:
    _patch_domain(monkeypatch, forecast_recall_load=None)

    response = _client().get(f"/admin/kronos/forecast?namespace=ns&hours_ahead={hours}")

    assert response.status_code == 422


def test_the_horizon_boundaries_are_accepted(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    async def _forecast(pool, namespace, *, hours_ahead):
        return _Forecast(namespace=namespace, hours_ahead=hours_ahead, expected=[])

    _patch_domain(monkeypatch, forecast_recall_load=_forecast)
    client = _client()

    assert client.get("/admin/kronos/forecast?namespace=ns&hours_ahead=1").status_code == 200
    assert client.get("/admin/kronos/forecast?namespace=ns&hours_ahead=720").status_code == 200


def test_a_non_integer_horizon_is_a_422(monkeypatch: pytest.MonkeyPatch, enabled) -> None:
    _patch_domain(monkeypatch, forecast_recall_load=None)

    response = _client().get("/admin/kronos/forecast?namespace=ns&hours_ahead=1.5")

    assert response.status_code == 422


def test_the_installed_but_disabled_body_is_the_plain_string(monkeypatch: pytest.MonkeyPatch) -> None:
    # End to end: a disabled profile answers {"detail": "KRONOS disabled in
    # this profile"} — a string body, not the {error, install} dict.
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: True)
    monkeypatch.setattr(kronos, "get_settings", lambda: _settings(enabled=False))

    response = _client().get("/admin/kronos/anomalies?namespace=ns")

    assert response.status_code == 503
    assert response.json() == {"detail": "KRONOS disabled in this profile"}


def test_the_missing_extra_body_is_the_install_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: False)

    response = _client().get("/admin/kronos/drift?namespace=ns")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error": "KRONOS not installed",
            "install": "pip install mnemos-core[kronos]  (or [server]/[ml]/[full] bundle)",
        }
    }
