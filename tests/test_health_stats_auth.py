from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mnemos.api.dependencies import configure_auth
from mnemos.api.routes.health import router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.pool = object()
    return app


def test_health_remains_public_when_auth_enabled():
    configure_auth({"enabled": True, "default_namespace": "default", "personal_user_id": "default"})
    try:
        response = TestClient(_app()).get("/health")
    finally:
        configure_auth({"enabled": False})

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_stats_requires_auth_when_auth_enabled():
    configure_auth({"enabled": True, "default_namespace": "default", "personal_user_id": "default"})
    try:
        response = TestClient(_app()).get("/stats")
    finally:
        configure_auth({"enabled": False})

    assert response.status_code == 401
