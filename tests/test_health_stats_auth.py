from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mnemos.api.dependencies import configure_auth
from mnemos.api.routes.health import router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    # The v3.x+ auth path goes through the backend-neutral OAuthRepository,
    # so the health/stats tests now stub a persistence_backend that
    # advertises the OAuth capability instead of a raw pool object.
    app.state.persistence_backend = _FakeOAuthBackend()
    return app


class _FakeOAuthBackend:
    """Minimal OAuth-capable persistence backend for auth-required tests."""

    _supports_oauth_persistence = True

    @property
    def oauth(self):
        return _NoAuthRepo()


class _NoAuthRepo:
    """Stub OAuthRepository whose lookups return None — auth path 401s."""

    async def list_enabled_providers(self, tx):
        return []

    async def get_provider(self, tx, name):
        return None

    async def provision_or_link_user(self, tx, *, provider, external_id, claims):
        raise NotImplementedError

    async def create_session(self, tx, **kwargs):
        raise NotImplementedError

    async def revoke_session(self, tx, session_id):
        return False

    async def revoke_all_sessions(self, tx, user_id):
        return 0

    async def get_identity_for_session(self, tx, session_id):
        return None

    async def lookup_api_key(self, tx, key_hash):
        return None

    async def touch_api_key(self, tx, key_id):
        return None

    async def resolve_active_session(self, tx, session_id, *, now):
        return None


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
