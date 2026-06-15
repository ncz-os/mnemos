"""KNEMON routers must 503 (missing-extra) when the knemon extra is disabled.

Mirrors the PANTHEON optional-extra gating so the core/add-on boundary is
enforceable: with the extra present (default — `()` probe = always installed)
behavior is unchanged; with it absent the routes return a clean 503 instead of
crashing. Forward-looking for the extract-to-ncz-os plan.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mnemos.api.extra_guards as extra_guards

_KNEMON_ROUTER_MODULES = (
    "mnemos.api.routes.knemon_router",
    "mnemos.api.routes.knemon_dashboard",
    "mnemos.api.routes.knemon_utilization",
    "mnemos.api.routes.ledger",
)


@pytest.mark.parametrize("module_path", _KNEMON_ROUTER_MODULES)
def test_knemon_router_503s_when_extra_disabled(module_path):
    import importlib

    router = importlib.import_module(module_path).router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    # Find any GET route on this router to probe the router-level dependency.
    get_path = next(
        (r.path for r in router.routes if "GET" in getattr(r, "methods", set())),
        None,
    )
    if get_path is None:
        pytest.skip(f"{module_path} exposes no GET route to probe")

    with patch.object(extra_guards, "is_extra_installed", lambda name: name != "knemon"):
        resp = client.get(get_path)
    assert resp.status_code == 503, f"{get_path} should 503 when knemon extra disabled"


def test_knemon_extra_installed_by_default():
    from mnemos.core.extras import is_extra_installed

    # () probe => always importable/installed in-tree; gating is a no-op today.
    assert is_extra_installed("knemon") is True
