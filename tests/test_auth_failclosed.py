from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mnemos.api import dependencies
from mnemos.core import config


def _reset(monkeypatch, profile: str, *, auth_enabled: bool | None = None,
           tmp_path=None):
    monkeypatch.setenv("MNEMOS_PROFILE", profile)
    # Pin the config to a non-existent path so the local config.toml
    # (which carries an [auth] section) doesn't leak into profile-only
    # profile/auth-default assertions.
    if tmp_path is not None:
        monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "missing.toml"))
    if auth_enabled is None:
        monkeypatch.delenv("MNEMOS_AUTH_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MNEMOS_AUTH_ENABLED", "true" if auth_enabled else "false")
    monkeypatch.setattr(config, "_settings", None)
    dependencies.configure_auth(None)


def test_server_profile_defaults_auth_enabled_and_rejects_missing_credentials(monkeypatch, tmp_path):
    _reset(monkeypatch, "server", tmp_path=tmp_path)
    app = FastAPI()
    app.state.pool = SimpleNamespace()

    @app.get("/v1/data")
    async def data(_user=Depends(dependencies.get_current_user)):
        return {"ok": True}

    resp = TestClient(app).get("/v1/data")
    assert resp.status_code == 401


def test_edge_profile_defaults_personal_mode(monkeypatch, tmp_path):
    # Edge profile sets auth_enabled = False in PROFILE_DEFAULTS but
    # the project-local config.toml pins [auth] enabled = true, which
    # would leak through.  Set MNEMOS_AUTH_ENABLED=false to assert
    # the profile-default path explicitly.
    _reset(monkeypatch, "edge", auth_enabled=False, tmp_path=tmp_path)
    app = FastAPI()

    @app.get("/v1/data")
    async def data(_user=Depends(dependencies.get_current_user)):
        return {"ok": True}

    resp = TestClient(app).get("/v1/data")
    assert resp.status_code == 200
