from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mnemos.api import dependencies
from mnemos.core import config


def _reset(monkeypatch, profile: str):
    monkeypatch.setenv("MNEMOS_PROFILE", profile)
    monkeypatch.delenv("MNEMOS_AUTH_ENABLED", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    dependencies.configure_auth(None)


class _FakeOAuthRepo:
    """Minimal OAuthRepository stand-in for the auth dependency tests."""

    def __init__(self) -> None:
        self.api_key_lookup_calls: list[str] = []
        self.session_lookup_calls: list[str] = []
        self.last_used_calls: list[str] = []
        self.resolve_session = None  # disable cookie path

    async def lookup_api_key(self, _tx, key_hash: str):
        self.api_key_lookup_calls.append(key_hash)
        return None

    async def touch_api_key(self, _tx, key_id) -> None:
        self.last_used_calls.append(str(key_id))

    async def resolve_active_session(self, _tx, _session_id, *, now):
        return None


def _auth_backend(oauth: _FakeOAuthRepo) -> SimpleNamespace:
    """Build a minimal persistence_backend shape exposing .oauth + .transactional."""

    @asynccontextmanager
    async def _tx_cm():
        yield SimpleNamespace()

    backend = SimpleNamespace(
        oauth=oauth,
        transactional=_tx_cm,
        _supports_oauth_persistence=True,
    )
    return backend


def test_server_profile_defaults_auth_enabled_and_rejects_missing_credentials(monkeypatch):
    _reset(monkeypatch, "server")
    oauth = _FakeOAuthRepo()
    app = FastAPI()
    app.state.persistence_backend = _auth_backend(oauth)

    @app.get("/v1/data")
    async def data(_user=Depends(dependencies.get_current_user)):
        return {"ok": True}

    resp = TestClient(app).get("/v1/data")
    assert resp.status_code == 401
    # Auth-disabled singleton path must not have been reached.
    assert oauth.api_key_lookup_calls == []


def test_edge_profile_defaults_personal_mode(monkeypatch):
    _reset(monkeypatch, "edge")
    app = FastAPI()

    @app.get("/v1/data")
    async def data(_user=Depends(dependencies.get_current_user)):
        return {"ok": True}

    resp = TestClient(app).get("/v1/data")
    assert resp.status_code == 200
