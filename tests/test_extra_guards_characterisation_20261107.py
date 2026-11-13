"""Characterisation tests (FB1117) for ``mnemos/api/extra_guards.py``.

Pins the behaviour of ``require_extra`` as it is today on ``master``
(``026d9cec``), not as it should be:

- ``require_extra(name, *, label=None)`` is a *factory*: it returns a
  ``fastapi.Depends`` object immediately, and the availability probe runs when
  the dependency is resolved (once per request), not when the factory is
  called. A test that swapped the extra's state after building the router
  therefore changes the answer, which is why the check is pinned to call
  time below.
- The inner dependency takes no parameters, so attaching the guard to a
  route never adds a query string or body field to it.
- A missing extra is a ``503`` whose ``detail`` is the dict from
  ``mnemos.core.extras.missing_extra_detail``: ``error`` names the extra
  upper-cased (or the ``label`` upper-cased) and ``install`` is the
  ``pip install mnemos-core[...]`` hint.
- ``label`` changes only the display name, never the install hint, and an
  empty ``label`` falls back to ``name`` because the code reads
  ``(label or name)``.
- An unknown extra name is a ``503`` like any other missing extra — the guard
  never turns a typo into a ``500``.
- ``name`` is passed through to the hint unchanged (no case folding), while
  the displayed error is upper-cased.

Nothing here asserts that this design is right. If any of it changes on
purpose, the pin changes with it.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.params import Depends as DependsParameter
from fastapi.testclient import TestClient

from mnemos.api import extra_guards


@pytest.fixture()
def installed(monkeypatch: pytest.MonkeyPatch):
    """Drive ``is_extra_installed`` from a mutable set of installed names."""

    state = {"installed": set()}

    def fake_is_extra_installed(name: str) -> bool:
        return name in state["installed"]

    monkeypatch.setattr(extra_guards, "is_extra_installed", fake_is_extra_installed)
    return state


def _resolve(dependency) -> None:
    """Call the function FastAPI would call for a resolved dependency."""
    dependency.dependency()


def test_it_returns_a_depends_object_not_the_callable(installed) -> None:
    # ``fastapi.Depends`` is a function that returns ``fastapi.params.Depends``
    # in this FastAPI; the factory returns that instance, not a bare callable.
    guard = extra_guards.require_extra("knemon")
    assert isinstance(guard, DependsParameter)
    # Depends.dependency is the inner function; the factory itself is not it.
    assert guard.dependency is not extra_guards.require_extra
    assert callable(guard.dependency)


def test_the_dependency_takes_no_parameters(installed) -> None:
    guard = extra_guards.require_extra("knemon")
    parameters = inspect.signature(guard.dependency).parameters
    assert list(parameters) == []


def test_a_missing_extra_raises_503_with_the_standard_detail(installed) -> None:
    guard = extra_guards.require_extra("knemon")

    with pytest.raises(HTTPException) as exc:
        _resolve(guard)

    assert exc.value.status_code == 503
    assert exc.value.detail == {
        "error": "KNEMON not installed",
        "install": "pip install mnemos-core[knemon]  (or [server]/[ml]/[full] bundle)",
    }


def test_an_installed_extra_passes_and_returns_none(installed) -> None:
    installed["installed"].add("knemon")
    guard = extra_guards.require_extra("knemon")

    assert _resolve(guard) is None


def test_the_probe_runs_at_call_time_not_at_factory_time(installed) -> None:
    # Built while missing, resolved while installed: the guard follows the
    # live state, which is what makes a router-level dependency correct.
    guard = extra_guards.require_extra("knemon")
    installed["installed"].add("knemon")

    assert _resolve(guard) is None

    installed["installed"].discard("knemon")
    with pytest.raises(HTTPException) as exc:
        _resolve(guard)

    assert exc.value.status_code == 503


def test_label_replaces_the_displayed_name_but_not_the_hint(installed) -> None:
    guard = extra_guards.require_extra("knemon", label="context fusion")

    with pytest.raises(HTTPException) as exc:
        _resolve(guard)

    assert exc.value.detail["error"] == "CONTEXT FUSION not installed"
    assert "mnemos-core[knemon]" in exc.value.detail["install"]


def test_an_empty_label_falls_back_to_the_name(installed) -> None:
    # ``(label or name)`` — an empty label is falsy, so it is not "labelled
    # with nothing"; pinned so the fallback is not changed by accident.
    guard = extra_guards.require_extra("knemon", label="")

    with pytest.raises(HTTPException) as exc:
        _resolve(guard)

    assert exc.value.detail["error"] == "KNEMON not installed"


def test_the_name_reaches_the_hint_verbatim_while_the_error_is_upper_cased(installed) -> None:
    guard = extra_guards.require_extra("Knemon")

    with pytest.raises(HTTPException) as exc:
        _resolve(guard)

    assert exc.value.detail["error"] == "KNEMON not installed"
    assert "mnemos-core[Knemon]" in exc.value.detail["install"]


def test_an_unknown_extra_is_a_503_not_a_500(installed) -> None:
    guard = extra_guards.require_extra("definitely-not-a-mnemos-extra")

    with pytest.raises(HTTPException) as exc:
        _resolve(guard)

    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "DEFINITELY-NOT-A-MNEMOS-EXTRA not installed"


def test_a_carved_out_extra_uses_its_carve_out_hint(installed) -> None:
    # ``hive`` has an UNAVAILABLE_EXTRAS entry: the status is still 503, the
    # install hint is the carve-out explanation rather than a pip line.
    guard = extra_guards.require_extra("hive")

    with pytest.raises(HTTPException) as exc:
        _resolve(guard)

    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "HIVE not installed"
    assert "STIPHOS" in exc.value.detail["install"]
    assert "pip install" not in exc.value.detail["install"]


def test_the_detail_is_a_dict_not_a_string(installed) -> None:
    guard = extra_guards.require_extra("knemon")

    with pytest.raises(HTTPException) as exc:
        _resolve(guard)

    assert isinstance(exc.value.detail, dict)
    assert set(exc.value.detail) == {"error", "install"}


def _app_with_guard(*dependencies) -> FastAPI:
    app = FastAPI()

    @app.get("/guarded", dependencies=list(dependencies))
    def _guarded() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_router_level_guard_returns_503_and_the_install_hint_in_the_body(installed) -> None:
    client = TestClient(_app_with_guard(extra_guards.require_extra("knemon")))

    response = client.get("/guarded")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error": "KNEMON not installed",
            "install": "pip install mnemos-core[knemon]  (or [server]/[ml]/[full] bundle)",
        }
    }


def test_router_level_guard_lets_the_route_run_when_the_extra_is_installed(installed) -> None:
    installed["installed"].add("knemon")
    client = TestClient(_app_with_guard(extra_guards.require_extra("knemon")))

    response = client.get("/guarded")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_the_guard_adds_no_parameters_to_the_route(installed) -> None:
    # A guard that declared parameters would show up in the OpenAPI document;
    # pin that the route's schema is unchanged by attaching one.
    installed["installed"].add("knemon")
    app = _app_with_guard(extra_guards.require_extra("knemon"))

    operation = app.openapi()["paths"]["/guarded"]["get"]
    assert operation.get("parameters", []) == []
    assert operation.get("requestBody") is None
